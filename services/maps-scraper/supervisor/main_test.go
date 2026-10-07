package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeScraper writes a CLI stand-in: it reads -input / -results like gosom and behaves per the query text.
func fakeScraper(t *testing.T) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "fake-scraper")
	script := `#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -input) in="$2"; shift;; -results) out="$2"; shift;; esac; shift; done
q=$(cat "$in")
case "$q" in
  *crash*) echo "playwright: target closed" >&2; exit 3;;
  *hang*) sleep 600;;
esac
printf 'title,website,place_id\nAgence A,https://a.example,p1\nAgence B,,p2\n' > "$out"
`
	if err := os.WriteFile(p, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

func server(t *testing.T) (*httptest.Server, *supervisor) {
	t.Helper()
	s := newSupervisor(config{Bin: fakeScraper(t), DataDir: t.TempDir(), Workers: 2, Concurrency: 1, Queue: 8,
		Retention: time.Hour}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := t.Context()
	for i := 0; i < 2; i++ {
		go s.worker(ctx)
	}
	ts := httptest.NewServer(s.routes())
	t.Cleanup(ts.Close)
	return ts, s
}

func create(t *testing.T, ts *httptest.Server, kw string, maxTime int) string {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"Name": "t", "keywords": []string{kw}, "lang": "en", "depth": 1, "max_time": maxTime})
	resp, err := http.Post(ts.URL+"/api/v1/jobs", "application/json", bytes.NewReader(body))
	if err != nil || resp.StatusCode != http.StatusCreated {
		t.Fatalf("create: %v %v", err, resp.Status)
	}
	var out map[string]string
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return out["id"]
}

func waitStatus(t *testing.T, ts *httptest.Server, id string, within time.Duration) job {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		resp, err := http.Get(ts.URL + "/api/v1/jobs/" + id)
		if err != nil {
			t.Fatal(err)
		}
		var j job
		_ = json.NewDecoder(resp.Body).Decode(&j)
		resp.Body.Close()
		if j.Status == "ok" || j.Status == "failed" {
			return j
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("job %s not finished in %s", id, within)
	return job{}
}

func TestLifecycleOkDownloadDelete(t *testing.T) {
	ts, _ := server(t)
	id := create(t, ts, "social media agency Miami", 60)
	j := waitStatus(t, ts, id, 5*time.Second)
	if j.Status != "ok" || j.Rows != 2 {
		t.Fatalf("got %+v", j)
	}
	resp, _ := http.Get(ts.URL + "/api/v1/jobs/" + id + "/download")
	csv, _ := io.ReadAll(resp.Body)
	if !strings.HasPrefix(string(csv), "title,website,place_id\nAgence A") {
		t.Fatalf("csv %q", csv)
	}
	req, _ := http.NewRequest(http.MethodDelete, ts.URL+"/api/v1/jobs/"+id, nil)
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != http.StatusOK {
		t.Fatal("delete")
	}
	if resp, _ := http.Get(ts.URL + "/api/v1/jobs/" + id); resp.StatusCode != http.StatusNotFound {
		t.Fatal("deleted job still visible") // the adapter recreates a vanished job
	}
}

func TestCrashFailsAloneAndNextJobsRun(t *testing.T) {
	ts, _ := server(t)
	bad := create(t, ts, "crash please", 60)
	if j := waitStatus(t, ts, bad, 5*time.Second); j.Status != "failed" || !strings.Contains(j.Error, "target closed") {
		t.Fatalf("got %+v", j)
	}
	for i := 0; i < 20; i++ { // the upstream web runner stopped dispatching after a few dozen jobs
		if j := waitStatus(t, ts, create(t, ts, "dentists Lyon", 60), 5*time.Second); j.Status != "ok" {
			t.Fatalf("job %d: %+v", i, j)
		}
	}
}

func TestOverrunIsKilledAndDoesNotBlockTheQueue(t *testing.T) {
	ts, _ := server(t)
	hang := create(t, ts, "hang forever", 30) // clamped minimum: 30 s budget
	ok := create(t, ts, "dentists Lyon", 60)
	if j := waitStatus(t, ts, ok, 5*time.Second); j.Status != "ok" {
		t.Fatalf("second worker blocked: %+v", j)
	}
	j := waitStatus(t, ts, hang, 45*time.Second)
	if j.Status != "ok" || j.Rows != 0 { // overrun with nothing written: an empty (valid) result, never stuck
		t.Fatalf("got %+v", j)
	}
}

func TestValidation(t *testing.T) {
	ts, _ := server(t)
	resp, _ := http.Post(ts.URL+"/api/v1/jobs", "application/json", strings.NewReader(`{"keywords":["  "]}`))
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("empty keywords accepted: %s", resp.Status)
	}
	if resp, _ := http.Get(ts.URL + "/api/v1/jobs"); resp.StatusCode != http.StatusOK {
		t.Fatal("healthcheck path")
	}
}
