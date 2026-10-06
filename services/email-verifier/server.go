package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	emailverifier "github.com/AfterShip/email-verifier"
	wappalyzer "github.com/projectdiscovery/wappalyzergo"
)

const (
	maxSmallBody = 16 << 10 // verify / catch-all payloads
	maxTechBody  = 4 << 20  // HTML + headers
)

var errBusy = errors.New("server busy")

// Server holds the shared, concurrency-safe verifier and fingerprint engine.
type Server struct {
	cfg      Config
	log      *slog.Logger
	verifier *emailverifier.Verifier
	wap      *wappalyzer.Wappalyze
	sem      chan struct{}
}

// NewServer builds the verifier (SMTP only when enabled) and loads the fingerprint database.
func NewServer(cfg Config, logger *slog.Logger) (*Server, error) {
	if cfg.MaxConcurrency <= 0 {
		cfg.MaxConcurrency = 16
	}
	if cfg.RequestTimeout <= 0 {
		cfg.RequestTimeout = 45 * time.Second
	}
	v := emailverifier.NewVerifier().
		HelloName(cfg.HeloDomain).
		FromEmail(cfg.FromAddress).
		ConnectTimeout(cfg.SMTPTimeout).
		OperationTimeout(cfg.SMTPTimeout).
		DisableGravatarCheck().
		DisableDomainSuggest()
	if cfg.SMTPEnabled {
		v = v.EnableSMTPCheck().EnableCatchAllCheck()
	} else {
		v = v.DisableSMTPCheck().DisableCatchAllCheck()
	}
	wap, err := wappalyzer.New()
	if err != nil {
		return nil, fmt.Errorf("load wappalyzer fingerprints: %w", err)
	}
	return &Server{cfg: cfg, log: logger, verifier: v, wap: wap, sem: make(chan struct{}, cfg.MaxConcurrency)}, nil
}

// Handler returns the routed handler wrapped with recovery, logging and auth middleware.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /v1/verify", s.handleVerify)
	mux.HandleFunc("POST /v1/catch-all", s.handleCatchAll)
	mux.HandleFunc("POST /v1/tech", s.handleTech)
	return s.recoverer(s.logRequests(s.auth(mux)))
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "smtp_enabled": s.cfg.SMTPEnabled})
}

// run executes fn under the concurrency limit. The slot is held until fn returns, even when the
// caller gives up first (the AfterShip library has no context support), so the bound is real.
func (s *Server) run(ctx context.Context, fn func()) error {
	select {
	case s.sem <- struct{}{}:
	case <-ctx.Done():
		return errBusy
	}
	done := make(chan struct{})
	go func() {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Error("worker panic", "panic", fmt.Sprint(rec))
			}
			<-s.sem
			close(done)
		}()
		fn()
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Server) runOrFail(w http.ResponseWriter, r *http.Request, fn func()) bool {
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.RequestTimeout)
	defer cancel()
	switch err := s.run(ctx, fn); {
	case err == nil:
		return true
	case errors.Is(err, errBusy):
		writeError(w, http.StatusServiceUnavailable, "too many concurrent requests")
	default:
		writeError(w, http.StatusGatewayTimeout, "verification timed out")
	}
	return false
}

// ---- middleware ----------------------------------------------------------------------------

func (s *Server) auth(next http.Handler) http.Handler {
	if s.cfg.Token == "" {
		return next
	}
	want := []byte("Bearer " + s.cfg.Token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		got := []byte(r.Header.Get("Authorization"))
		if subtle.ConstantTimeCompare(got, want) != 1 {
			writeError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		next.ServeHTTP(w, r)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (s *Server) logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		if r.URL.Path == "/healthz" && rec.status == http.StatusOK {
			return
		}
		s.log.Info("request", "method", r.Method, "path", r.URL.Path, "status", rec.status,
			"duration_ms", time.Since(start).Milliseconds())
	})
}

func (s *Server) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Error("handler panic", "panic", fmt.Sprint(rec), "path", r.URL.Path)
				writeError(w, http.StatusInternalServerError, "internal error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// ---- JSON helpers ---------------------------------------------------------------------------

func decodeJSON(w http.ResponseWriter, r *http.Request, limit int64, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func strPtr(s string) *string { return &s }

func boolPtr(b bool) *bool { return &b }

func normalizeDomain(d string) string {
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(d)), ".")
}
