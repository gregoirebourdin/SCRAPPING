package main

import (
	"net/http"
	"strings"
)

// SMTPResult mirrors the AfterShip SMTP block; CatchAll is null when unknown.
type SMTPResult struct {
	Enabled     bool    `json:"enabled"`
	HostExists  bool    `json:"host_exists"`
	Deliverable bool    `json:"deliverable"`
	FullInbox   bool    `json:"full_inbox"`
	CatchAll    *bool   `json:"catch_all"`
	Disabled    bool    `json:"disabled"`
	Error       *string `json:"error"`
}

// VerifyResponse is the /v1/verify contract consumed by scout.email.verifier.service.
type VerifyResponse struct {
	Email       string     `json:"email"`
	SyntaxValid bool       `json:"syntax_valid"`
	HasMX       bool       `json:"has_mx"`
	MXHosts     []string   `json:"mx_hosts"`
	SMTP        SMTPResult `json:"smtp"`
	Disposable  bool       `json:"disposable"`
	RoleAccount bool       `json:"role_account"`
	Free        bool       `json:"free"`
	Reachable   string     `json:"reachable"` // yes | no | unknown
	Error       *string    `json:"error"`
}

// CatchAllResponse is the /v1/catch-all contract.
type CatchAllResponse struct {
	Domain      string  `json:"domain"`
	CatchAll    *bool   `json:"catch_all"`
	SMTPEnabled bool    `json:"smtp_enabled"`
	Error       *string `json:"error,omitempty"`
}

func (s *Server) handleVerify(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Email string `json:"email"`
	}
	if !decodeJSON(w, r, maxSmallBody, &req) {
		return
	}
	email := strings.ToLower(strings.TrimSpace(req.Email))
	if email == "" || len(email) > 320 {
		writeError(w, http.StatusBadRequest, "email is required")
		return
	}
	var resp VerifyResponse
	if s.runOrFail(w, r, func() { resp = s.verifyEmail(email) }) {
		writeJSON(w, http.StatusOK, resp)
	}
}

// verifyEmail composes the library's checks so MX hosts are returned without a second lookup.
// Disposable domains are never contacted; SMTP runs only when enabled.
func (s *Server) verifyEmail(email string) VerifyResponse {
	resp := VerifyResponse{
		Email:     email,
		MXHosts:   []string{},
		Reachable: "unknown",
		SMTP:      SMTPResult{Enabled: s.cfg.SMTPEnabled},
	}
	syntax := s.verifier.ParseAddress(email)
	if !syntax.Valid {
		resp.Reachable = "no"
		resp.Error = strPtr("invalid syntax")
		return resp
	}
	resp.SyntaxValid = true
	resp.Free = s.verifier.IsFreeDomain(syntax.Domain)
	resp.RoleAccount = s.verifier.IsRoleAccount(syntax.Username)
	resp.Disposable = s.verifier.IsDisposable(syntax.Domain)
	if resp.Disposable {
		return resp
	}

	mx, err := s.verifier.CheckMX(syntax.Domain)
	if err != nil {
		resp.Error = strPtr(err.Error())
		if strings.Contains(strings.ToLower(err.Error()), "no such host") {
			resp.Reachable = "no"
		}
		return resp
	}
	resp.HasMX = mx.HasMXRecord
	for _, rec := range mx.Records {
		if host := normalizeDomain(rec.Host); host != "" {
			resp.MXHosts = append(resp.MXHosts, host)
		}
	}
	if !resp.HasMX {
		resp.Reachable = "no"
		return resp
	}
	if !s.cfg.SMTPEnabled {
		return resp
	}

	smtp, err := s.verifier.CheckSMTP(syntax.Domain, syntax.Username)
	if smtp != nil {
		resp.SMTP.HostExists = smtp.HostExists
	}
	if err != nil {
		resp.SMTP.Error = strPtr(err.Error())
		return resp
	}
	if smtp == nil {
		return resp
	}
	resp.SMTP.Deliverable = smtp.Deliverable
	resp.SMTP.FullInbox = smtp.FullInbox
	resp.SMTP.Disabled = smtp.Disabled
	resp.SMTP.CatchAll = boolPtr(smtp.CatchAll)
	switch {
	case smtp.Deliverable:
		resp.Reachable = "yes"
	case smtp.CatchAll:
		resp.Reachable = "unknown"
	default:
		resp.Reachable = "no"
	}
	return resp
}

func (s *Server) handleCatchAll(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Domain string `json:"domain"`
	}
	if !decodeJSON(w, r, maxSmallBody, &req) {
		return
	}
	domain := normalizeDomain(req.Domain)
	if domain == "" || len(domain) > 253 || strings.ContainsAny(domain, "@/ ") || !strings.Contains(domain, ".") {
		writeError(w, http.StatusBadRequest, "a valid domain is required")
		return
	}
	resp := CatchAllResponse{Domain: domain, SMTPEnabled: s.cfg.SMTPEnabled}
	if !s.cfg.SMTPEnabled {
		writeJSON(w, http.StatusOK, resp)
		return
	}
	if s.runOrFail(w, r, func() {
		// An empty username makes the library stop right after the random-recipient probe.
		smtp, err := s.verifier.CheckSMTP(domain, "")
		switch {
		case err != nil:
			resp.Error = strPtr(err.Error())
		case smtp != nil:
			resp.CatchAll = boolPtr(smtp.CatchAll)
		}
	}) {
		writeJSON(w, http.StatusOK, resp)
	}
}
