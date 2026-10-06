package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

var (
	baseOnce sync.Once
	base     *Server
	baseErr  error
)

// newTestServer shares one fingerprint database across tests (loading it is the slow part).
func newTestServer(t *testing.T, token string) *httptest.Server {
	t.Helper()
	baseOnce.Do(func() {
		base, baseErr = NewServer(Config{
			SMTPEnabled:    false,
			HeloDomain:     "scout.test",
			FromAddress:    "verify@scout.test",
			MaxConcurrency: 4,
			SMTPTimeout:    time.Second,
			RequestTimeout: 5 * time.Second,
		}, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	})
	if baseErr != nil {
		t.Fatalf("NewServer: %v", baseErr)
	}
	srv := *base
	srv.cfg.Token = token
	srv.sem = make(chan struct{}, srv.cfg.MaxConcurrency)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts
}

func doJSON(t *testing.T, ts *httptest.Server, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, ts.URL+path, rd)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := ts.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode %s %s: %v", method, path, err)
	}
	return resp.StatusCode, out
}

func TestHealthz(t *testing.T) {
	ts := newTestServer(t, "secret")
	code, body := doJSON(t, ts, http.MethodGet, "/healthz", "", nil)
	if code != http.StatusOK || body["ok"] != true || body["smtp_enabled"] != false {
		t.Fatalf("healthz = %d %v", code, body)
	}
}

func TestAuth(t *testing.T) {
	ts := newTestServer(t, "secret")
	payload := map[string]string{"email": "not-an-email"}
	if code, _ := doJSON(t, ts, http.MethodPost, "/v1/verify", "", payload); code != http.StatusUnauthorized {
		t.Fatalf("missing token: got %d", code)
	}
	if code, _ := doJSON(t, ts, http.MethodPost, "/v1/verify", "wrong", payload); code != http.StatusUnauthorized {
		t.Fatalf("wrong token: got %d", code)
	}
	if code, _ := doJSON(t, ts, http.MethodPost, "/v1/verify", "secret", payload); code != http.StatusOK {
		t.Fatalf("valid token: got %d", code)
	}
}

func TestVerifyInvalidSyntax(t *testing.T) {
	ts := newTestServer(t, "")
	code, body := doJSON(t, ts, http.MethodPost, "/v1/verify", "", map[string]string{"email": "Not An Email"})
	if code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	smtp := body["smtp"].(map[string]any)
	if body["syntax_valid"] != false || body["has_mx"] != false || body["reachable"] != "no" ||
		smtp["enabled"] != false || smtp["catch_all"] != nil {
		t.Fatalf("unexpected body %v", body)
	}
}

func TestVerifyDisposableSkipsNetwork(t *testing.T) {
	ts := newTestServer(t, "")
	code, body := doJSON(t, ts, http.MethodPost, "/v1/verify", "", map[string]string{"email": "Someone@Mailinator.com"})
	if code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	if body["email"] != "someone@mailinator.com" || body["syntax_valid"] != true || body["disposable"] != true ||
		body["has_mx"] != false || body["reachable"] != "unknown" {
		t.Fatalf("unexpected body %v", body)
	}
	if hosts := body["mx_hosts"].([]any); len(hosts) != 0 {
		t.Fatalf("disposable domain must not be resolved: %v", hosts)
	}
}

func TestVerifyRequiresEmail(t *testing.T) {
	ts := newTestServer(t, "")
	if code, _ := doJSON(t, ts, http.MethodPost, "/v1/verify", "", map[string]string{}); code != http.StatusBadRequest {
		t.Fatalf("got %d", code)
	}
}

func TestCatchAllWithoutSMTP(t *testing.T) {
	ts := newTestServer(t, "")
	code, body := doJSON(t, ts, http.MethodPost, "/v1/catch-all", "", map[string]string{"domain": "Example.FR."})
	if code != http.StatusOK || body["domain"] != "example.fr" || body["catch_all"] != nil || body["smtp_enabled"] != false {
		t.Fatalf("got %d %v", code, body)
	}
	if code, _ := doJSON(t, ts, http.MethodPost, "/v1/catch-all", "", map[string]string{"domain": "a@b"}); code != http.StatusBadRequest {
		t.Fatalf("invalid domain: got %d", code)
	}
}

func findTech(t *testing.T, body map[string]any, name string) map[string]any {
	t.Helper()
	for _, raw := range body["technologies"].([]any) {
		tech := raw.(map[string]any)
		if tech["name"] == name {
			return tech
		}
	}
	t.Fatalf("%s not detected in %v", name, body["technologies"])
	return nil
}

func hasCategory(tech map[string]any, cat string) bool {
	for _, c := range tech["categories"].([]any) {
		if c == cat {
			return true
		}
	}
	return false
}

func TestTechDetectsWordPressWithVersion(t *testing.T) {
	ts := newTestServer(t, "")
	html := `<!doctype html><html><head>
<meta name="generator" content="WordPress 6.4.2">
<link rel="stylesheet" href="https://agence-x.fr/wp-content/themes/astra/style.css" media="all">
<script src="https://agence-x.fr/wp-includes/js/jquery/jquery.min.js"></script>
</head><body>Agence X</body></html>`
	code, body := doJSON(t, ts, http.MethodPost, "/v1/tech", "", map[string]any{
		"url":     "https://agence-x.fr/",
		"headers": map[string]any{"Content-Type": "text/html; charset=UTF-8", "Link": `<https://agence-x.fr/wp-json/>; rel="https://api.w.org/"`},
		"html":    html,
	})
	if code != http.StatusOK {
		t.Fatalf("status %d %v", code, body)
	}
	wp := findTech(t, body, "WordPress")
	if wp["version"] != "6.4.2" || !hasCategory(wp, "CMS") {
		t.Fatalf("unexpected WordPress entry %v", wp)
	}
}

func TestTechDetectsShopify(t *testing.T) {
	ts := newTestServer(t, "")
	html := `<html><head><meta name="shopify-digital-wallet" content="/123/digital_wallets/dialog">
<link rel="stylesheet" href="https://cdn.shopify.com/s/files/1/theme.css"></head><body>Shop</body></html>`
	code, body := doJSON(t, ts, http.MethodPost, "/v1/tech", "", map[string]any{
		"url":     "https://boutique.example/",
		"headers": map[string]any{"Content-Type": "text/html", "Powered-By": "Shopify", "Set-Cookie": []string{"_shopify_y=abc; path=/"}},
		"html":    html,
	})
	if code != http.StatusOK {
		t.Fatalf("status %d %v", code, body)
	}
	shop := findTech(t, body, "Shopify")
	if !hasCategory(shop, "Ecommerce") {
		t.Fatalf("unexpected Shopify entry %v", shop)
	}
}

func TestUnknownRouteAndMethod(t *testing.T) {
	ts := newTestServer(t, "")
	resp, err := ts.Client().Get(ts.URL + "/v1/verify")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /v1/verify = %d", resp.StatusCode)
	}
}
