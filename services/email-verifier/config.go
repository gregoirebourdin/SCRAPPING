package main

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is read once from the environment at startup.
type Config struct {
	Port           string        // PORT (default 8080)
	Token          string        // VERIFIER_TOKEN: bearer token required on /v1/* when set
	SMTPEnabled    bool          // SMTP_ENABLED: RCPT probing + catch-all detection (needs outbound port 25)
	HeloDomain     string        // SMTP_HELO_DOMAIN: EHLO name
	FromAddress    string        // SMTP_FROM: MAIL FROM address
	MaxConcurrency int           // MAX_CONCURRENCY: simultaneous verifications/fingerprints (default 16)
	SMTPTimeout    time.Duration // SMTP_TIMEOUT: connect + per-command timeout (default 10s)
	RequestTimeout time.Duration // REQUEST_TIMEOUT: end-to-end budget per request (default 45s)
}

// LoadConfig reads the configuration from environment variables with safe defaults.
func LoadConfig() Config {
	return Config{
		Port:           envString("PORT", "8080"),
		Token:          strings.TrimSpace(os.Getenv("VERIFIER_TOKEN")),
		SMTPEnabled:    envBool("SMTP_ENABLED", false),
		HeloDomain:     envString("SMTP_HELO_DOMAIN", "scout.example"),
		FromAddress:    envString("SMTP_FROM", "verify@scout.example"),
		MaxConcurrency: envInt("MAX_CONCURRENCY", 16),
		SMTPTimeout:    envDuration("SMTP_TIMEOUT", 10*time.Second),
		RequestTimeout: envDuration("REQUEST_TIMEOUT", 45*time.Second),
	}
}

func envString(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envBool(key string, def bool) bool {
	if b, err := strconv.ParseBool(strings.TrimSpace(os.Getenv(key))); err == nil {
		return b
	}
	return def
}

func envInt(key string, def int) int {
	if n, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil && n > 0 {
		return n
	}
	return def
}

// envDuration accepts Go durations ("10s") or plain seconds ("10").
func envDuration(key string, def time.Duration) time.Duration {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	if d, err := time.ParseDuration(v); err == nil && d > 0 {
		return d
	}
	if s, err := strconv.ParseFloat(v, 64); err == nil && s > 0 {
		return time.Duration(s * float64(time.Second))
	}
	return def
}
