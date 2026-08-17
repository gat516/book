package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

var (
	ErrAskUnavailable = errors.New("ask-ai unavailable")
	ErrAskRejected    = errors.New("ask-ai admission rejected")
)

type AskClient interface {
	Ask(context.Context, string, string, int) (json.RawMessage, error)
}

type askHTTPClient struct {
	url   string
	token string
	http  *http.Client
}

func newAskClient(cfg Config) AskClient {
	return &askHTTPClient{url: strings.TrimRight(cfg.AskAIURL, "/") + "/ask", token: cfg.AskAIInternalToken, http: &http.Client{Timeout: time.Duration(cfg.AskAITimeoutSeconds) * time.Second}}
}

func (c *askHTTPClient) Ask(ctx context.Context, novelID, question string, at int) (json.RawMessage, error) {
	body, _ := json.Marshal(map[string]any{"novel_id": novelID, "question": question, "at": at})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build ask request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrAskUnavailable, err)
	}
	defer resp.Body.Close()
	var result json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("invalid ask-ai response: %w", err)
	}
	switch resp.StatusCode {
	case http.StatusOK:
		return result, nil
	case http.StatusTooManyRequests, http.StatusServiceUnavailable:
		return nil, ErrAskRejected
	default:
		return nil, fmt.Errorf("ask-ai returned %d", resp.StatusCode)
	}
}

type askRequest struct {
	Question string `json:"question"`
	At       *int   `json:"at"`
}

func (a *API) postAsk(w http.ResponseWriter, r *http.Request) {
	var request askRequest
	if err := decodeJSON(r, &request); err != nil || strings.TrimSpace(request.Question) == "" || (request.At != nil && *request.At < 0) {
		prepareReaderResponse(w)
		writeError(w, http.StatusBadRequest, "question and nonnegative at are required")
		return
	}
	_, novelID, at, ok := a.gateAt(w, r, request.At)
	if !ok {
		return
	}
	if a.ask == nil {
		writeError(w, http.StatusServiceUnavailable, "ask-ai unavailable")
		return
	}
	response, err := a.ask.Ask(r.Context(), novelID, request.Question, at)
	if errors.Is(err, ErrAskUnavailable) || errors.Is(err, ErrAskRejected) {
		writeError(w, http.StatusServiceUnavailable, "ask-ai unavailable")
		return
	}
	if err != nil {
		writeError(w, http.StatusBadGateway, "ask-ai failed")
		return
	}
	var envelope struct {
		At int `json:"at"`
	}
	if err := json.Unmarshal(response, &envelope); err != nil || envelope.At != at {
		writeError(w, http.StatusBadGateway, "ask-ai returned an invalid gate")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(response)
}
