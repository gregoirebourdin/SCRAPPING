package main

import (
	"encoding/json"
	"net/http"
	"sort"
	"strings"

	wappalyzer "github.com/projectdiscovery/wappalyzergo"
)

// headerValues accepts either "value" or ["v1", "v2"] for each response header.
type headerValues []string

func (h *headerValues) UnmarshalJSON(b []byte) error {
	var one string
	if err := json.Unmarshal(b, &one); err == nil {
		*h = []string{one}
		return nil
	}
	var many []string
	if err := json.Unmarshal(b, &many); err != nil {
		return err
	}
	*h = many
	return nil
}

// TechRequest is a page already fetched by the crawler (Service B never fetches URLs itself).
type TechRequest struct {
	URL     string                  `json:"url"`
	Headers map[string]headerValues `json:"headers"`
	HTML    string                  `json:"html"`
}

// Technology is one detected product; Version is empty when the fingerprint exposes none.
type Technology struct {
	Name       string   `json:"name"`
	Categories []string `json:"categories"`
	Version    string   `json:"version"`
}

func (s *Server) handleTech(w http.ResponseWriter, r *http.Request) {
	var req TechRequest
	if !decodeJSON(w, r, maxTechBody, &req) {
		return
	}
	headers := make(map[string][]string, len(req.Headers))
	for k, v := range req.Headers {
		headers[k] = v
	}
	var techs []Technology
	if s.runOrFail(w, r, func() { techs = s.detect(headers, []byte(req.HTML)) }) {
		writeJSON(w, http.StatusOK, map[string]any{"url": req.URL, "technologies": techs})
	}
}

// detect fingerprints headers + HTML. wappalyzergo reports versioned apps as "Name:Version".
func (s *Server) detect(headers map[string][]string, body []byte) []Technology {
	apps := s.wap.Fingerprint(headers, body)
	compiled := s.wap.GetCompiledFingerprints().Apps
	out := make([]Technology, 0, len(apps))
	for key := range apps {
		name, version := key, ""
		if _, ok := compiled[key]; !ok {
			if n, v, found := strings.Cut(key, ":"); found {
				if _, ok := compiled[n]; ok {
					name, version = n, v
				}
			}
		}
		categories := []string{}
		if fp, ok := compiled[name]; ok {
			if cats := wappalyzer.AppInfoFromFingerprint(fp).Categories; len(cats) > 0 {
				categories = cats
			}
		}
		sort.Strings(categories)
		out = append(out, Technology{Name: name, Categories: categories, Version: version})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}
