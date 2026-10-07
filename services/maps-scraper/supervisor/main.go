// mapsd: the job supervisor in front of the gosom/google-maps-scraper CLI.
//
// gosom's own web runner (-web) stops dispatching after a few dozen jobs (upstream "supervisor stops dispatching"
// issue): jobs then stay "pending" forever. mapsd keeps the same REST contract as the web runner (POST/GET/DELETE
// /api/v1/jobs, GET /api/v1/jobs/{id}/download) but runs every job as a fresh CLI process in its own process
// group, killed with all its browsers when it overruns. Nothing long-lived can wedge: one bad job fails alone.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type jobRequest struct {
	Name     string   `json:"Name"`
	Keywords []string `json:"keywords"`
	Lang     string   `json:"lang"`
	Zoom     int      `json:"zoom"`
	Lat      string   `json:"lat"`
	Lon      string   `json:"lon"`
	FastMode bool     `json:"fast_mode"`
	Radius   int      `json:"radius"`
	Depth    int      `json:"depth"`
	Email    bool     `json:"email"`
	MaxTime  int      `json:"max_time"`
	Proxies  []string `json:"proxies"`
}

type job struct {
	ID       string     `json:"ID"`
	Name     string     `json:"Name"`
	Date     time.Time  `json:"Date"`
	Status   string     `json:"Status"` // pending | working | ok | failed
	Data     jobRequest `json:"Data"`
	Error    string     `json:"Error,omitempty"`
	Rows     int        `json:"Rows"`
	finished time.Time
	dir      string
}

type config struct {
	Bin         string
	DataDir     string
	Workers     int
	Concurrency int
	Queue       int
	Retention   time.Duration
}

type supervisor struct {
	cfg   config
	log   *slog.Logger
	mu    sync.Mutex
	jobs  map[string]*job
	queue chan string
}

func newSupervisor(cfg config, log *slog.Logger) *supervisor {
	return &supervisor{cfg: cfg, log: log, jobs: map[string]*job{}, queue: make(chan string, cfg.Queue)}
}

func newID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func (s *supervisor) submit(req jobRequest) (*job, error) {
	var kws []string
	for _, k := range req.Keywords {
		if k = strings.TrimSpace(strings.ReplaceAll(k, "\n", " ")); k != "" {
			kws = append(kws, k)
		}
	}
	if len(kws) == 0 {
		return nil, errors.New("keywords required")
	}
	if len(kws) > 20 {
		kws = kws[:20]
	}
	req.Keywords = kws
	req.Depth = clamp(req.Depth, 1, 10)
	req.MaxTime = clamp(req.MaxTime, 30, 900)
	if req.Lang == "" {
		req.Lang = "en"
	}
	j := &job{ID: newID(), Name: req.Name, Date: time.Now().UTC(), Status: "pending", Data: req}
	s.mu.Lock()
	s.jobs[j.ID] = j
	s.mu.Unlock()
	select {
	case s.queue <- j.ID:
		return j, nil
	default:
		s.mu.Lock()
		delete(s.jobs, j.ID)
		s.mu.Unlock()
		return nil, errors.New("queue full")
	}
}

func (s *supervisor) get(id string) (job, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return job{}, false
	}
	return *j, true
}

func (s *supervisor) update(id string, fn func(*job)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if j, ok := s.jobs[id]; ok {
		fn(j)
	}
}

func (s *supervisor) remove(id string) bool {
	s.mu.Lock()
	j, ok := s.jobs[id]
	delete(s.jobs, id)
	s.mu.Unlock()
	if ok && j.dir != "" {
		_ = os.RemoveAll(j.dir)
	}
	return ok
}

func (s *supervisor) worker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case id := <-s.queue:
			if j, ok := s.get(id); ok { // deleted while queued → skip
				s.run(ctx, j)
			}
		}
	}
}

// run executes one job as a fresh CLI process; partial results of an overrun job are still delivered.
func (s *supervisor) run(ctx context.Context, j job) {
	dir, err := os.MkdirTemp(s.cfg.DataDir, "job-")
	if err != nil {
		s.finish(j.ID, "", 0, err)
		return
	}
	s.update(j.ID, func(x *job) { x.Status, x.dir = "working", dir })
	in, out := filepath.Join(dir, "queries.txt"), filepath.Join(dir, "results.csv")
	if err := os.WriteFile(in, []byte(strings.Join(j.Data.Keywords, "\n")+"\n"), 0o600); err != nil {
		s.finish(j.ID, dir, 0, err)
		return
	}
	args := []string{
		"-input", in, "-results", out,
		"-depth", strconv.Itoa(j.Data.Depth),
		"-c", strconv.Itoa(s.cfg.Concurrency),
		"-lang", j.Data.Lang,
		"-exit-on-inactivity", "1m",
	}
	if j.Data.Lat != "" && j.Data.Lon != "" {
		args = append(args, "-geo", j.Data.Lat+","+j.Data.Lon,
			"-zoom", strconv.Itoa(clamp(j.Data.Zoom, 1, 21)), "-radius", strconv.Itoa(clamp(j.Data.Radius, 100, 100000)))
	}
	if j.Data.FastMode {
		args = append(args, "-fast-mode")
	}
	if j.Data.Email {
		args = append(args, "-email")
	}
	if len(j.Data.Proxies) > 0 {
		args = append(args, "-proxies", strings.Join(j.Data.Proxies, ","))
	}
	runCtx, cancel := context.WithTimeout(ctx, time.Duration(j.Data.MaxTime)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(runCtx, s.cfg.Bin, args...)
	cmd.Dir = dir
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) } // the browsers too
	cmd.WaitDelay = 10 * time.Second
	tail := &tailBuffer{max: 4096}
	cmd.Stdout, cmd.Stderr = tail, tail
	started := time.Now()
	runErr := cmd.Run()
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) // stray browser processes never outlive their job
	}
	rows := countRows(out)
	s.log.Info("job done", "id", j.ID, "rows", rows, "secs", int(time.Since(started).Seconds()), "err", errString(runErr))
	if runErr != nil && rows == 0 && !errors.Is(runCtx.Err(), context.DeadlineExceeded) {
		s.finish(j.ID, dir, 0, fmt.Errorf("%v: %s", runErr, tail.String()))
		return
	}
	if _, statErr := os.Stat(out); statErr != nil {
		_ = os.WriteFile(out, nil, 0o600) // no results is a valid answer
	}
	s.finish(j.ID, dir, rows, nil)
}

func (s *supervisor) finish(id, dir string, rows int, err error) {
	s.update(id, func(x *job) {
		x.dir, x.Rows, x.finished = dir, rows, time.Now()
		if err != nil {
			x.Status, x.Error = "failed", truncate(err.Error(), 2000)
		} else {
			x.Status = "ok"
		}
	})
}

// gc drops finished jobs nobody downloaded/deleted after the retention period.
func (s *supervisor) gc(ctx context.Context) {
	t := time.NewTicker(5 * time.Minute)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.mu.Lock()
			var stale []string
			for id, j := range s.jobs {
				if !j.finished.IsZero() && time.Since(j.finished) > s.cfg.Retention {
					stale = append(stale, id)
				}
			}
			s.mu.Unlock()
			for _, id := range stale {
				s.remove(id)
			}
		}
	}
}

func (s *supervisor) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "queued": len(s.queue)})
	})
	mux.HandleFunc("GET /api/v1/jobs", func(w http.ResponseWriter, _ *http.Request) {
		s.mu.Lock()
		out := make([]job, 0, len(s.jobs))
		for _, j := range s.jobs {
			out = append(out, *j)
		}
		s.mu.Unlock()
		sort.Slice(out, func(a, b int) bool { return out[a].Date.After(out[b].Date) })
		writeJSON(w, http.StatusOK, out)
	})
	mux.HandleFunc("POST /api/v1/jobs", func(w http.ResponseWriter, r *http.Request) {
		var req jobRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"message": "invalid JSON"})
			return
		}
		j, err := s.submit(req)
		switch {
		case err != nil && err.Error() == "queue full":
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"message": err.Error()})
		case err != nil:
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"message": err.Error()})
		default:
			writeJSON(w, http.StatusCreated, map[string]string{"id": j.ID})
		}
	})
	mux.HandleFunc("GET /api/v1/jobs/{id}", func(w http.ResponseWriter, r *http.Request) {
		if j, ok := s.get(r.PathValue("id")); ok {
			writeJSON(w, http.StatusOK, j)
			return
		}
		writeJSON(w, http.StatusNotFound, map[string]string{"message": "not found"})
	})
	mux.HandleFunc("GET /api/v1/jobs/{id}/download", func(w http.ResponseWriter, r *http.Request) {
		j, ok := s.get(r.PathValue("id"))
		if !ok || j.Status != "ok" {
			writeJSON(w, http.StatusNotFound, map[string]string{"message": "not ready"})
			return
		}
		w.Header().Set("Content-Type", "text/csv")
		http.ServeFile(w, r, filepath.Join(j.dir, "results.csv"))
	})
	mux.HandleFunc("DELETE /api/v1/jobs/{id}", func(w http.ResponseWriter, r *http.Request) {
		if s.remove(r.PathValue("id")) {
			w.WriteHeader(http.StatusOK)
			return
		}
		writeJSON(w, http.StatusNotFound, map[string]string{"message": "not found"})
	})
	return mux
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg := config{
		Bin:         envStr("SCRAPER_BIN", "google-maps-scraper"),
		DataDir:     envStr("DATA_DIR", filepath.Join(os.TempDir(), "mapsd")),
		Workers:     envInt("WORKERS", 2),
		Concurrency: envInt("SCRAPER_CONCURRENCY", 2),
		Queue:       envInt("QUEUE_SIZE", 256),
		Retention:   2 * time.Hour,
	}
	if err := os.MkdirAll(cfg.DataDir, 0o700); err != nil {
		log.Error("data dir", "err", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	s := newSupervisor(cfg, log)
	for i := 0; i < cfg.Workers; i++ {
		go s.worker(ctx)
	}
	go s.gc(ctx)
	srv := &http.Server{Addr: ":" + envStr("PORT", "8080"), Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shut, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shut)
	}()
	log.Info("mapsd listening", "addr", srv.Addr, "workers", cfg.Workers, "concurrency", cfg.Concurrency)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("server", "err", err)
		os.Exit(1)
	}
}

// ---- helpers ----------------------------------------------------------------------------------

type tailBuffer struct {
	mu  sync.Mutex
	buf []byte
	max int
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.max {
		t.buf = t.buf[len(t.buf)-t.max:]
	}
	return len(p), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

func countRows(path string) int {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	lines := strings.Count(strings.TrimRight(string(b), "\n"), "\n")
	if len(strings.TrimSpace(string(b))) == 0 {
		return 0
	}
	return lines // header excluded
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func envStr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(k))); err == nil && v > 0 {
		return v
	}
	return def
}
