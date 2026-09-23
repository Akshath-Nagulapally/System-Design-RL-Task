package kv

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type stubStore struct {
	value   string
	version int64
	found   bool
	err     error
	expect  *int64
}

func (s *stubStore) Get(context.Context, string) (string, int64, bool, error) {
	return s.value, s.version, s.found, s.err
}
func (s *stubStore) Put(_ context.Context, _ string, value string, expected *int64) (int64, bool, error) {
	s.value, s.expect = value, expected
	return s.version, s.found, s.err
}
func (s *stubStore) Delete(context.Context, string) error { return s.err }
func (s *stubStore) Ready(context.Context) error          { return s.err }

func TestHTTPContract(t *testing.T) {
	store := &stubStore{value: "hello", version: 42, found: true}
	handler := NewHandler(store, time.Second)

	request := httptest.NewRequest(http.MethodGet, "/v1/kv/example", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Header().Get("ETag") != `"42"` {
		t.Fatalf("GET status=%d ETag=%q", response.Code, response.Header().Get("ETag"))
	}
	var body map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || body["value"] != "hello" || body["version"] != "42" {
		t.Fatalf("GET body=%s err=%v", response.Body.String(), err)
	}

	request = httptest.NewRequest(http.MethodPut, "/v1/kv/example", strings.NewReader(`{"value":"updated"}`))
	request.Header.Set("If-Match", `"42"`)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || store.expect == nil || *store.expect != 42 || store.value != "updated" {
		t.Fatalf("conditional PUT status=%d expected=%v value=%q", response.Code, store.expect, store.value)
	}

	store.found = false
	request = httptest.NewRequest(http.MethodPut, "/v1/kv/example", strings.NewReader(`{"value":"updated"}`))
	request.Header.Set("If-Match", `"42"`)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusPreconditionFailed {
		t.Fatalf("conditional mismatch status=%d", response.Code)
	}

	store.err = errors.New("lost quorum")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("unavailable health status=%d", response.Code)
	}
}

func TestRejectsInvalidInput(t *testing.T) {
	handler := NewHandler(&stubStore{}, time.Second)
	cases := []struct {
		method, path, body, ifMatch string
	}{
		{http.MethodGet, "/v1/kv/a/b", "", ""},
		{http.MethodGet, "/v1/kv/", "", ""},
		{http.MethodPut, "/v1/kv/key", `{"value":42}`, ""},
		{http.MethodPut, "/v1/kv/key", `{"value":"x"}{"value":"y"}`, ""},
		{http.MethodPut, "/v1/kv/key", `{"value":"x"}`, `"not-a-version"`},
		{http.MethodPut, "/v1/kv/key", `{"value":"` + strings.Repeat("x", 4097) + `"}`, ""},
	}
	for _, test := range cases {
		request := httptest.NewRequest(test.method, test.path, strings.NewReader(test.body))
		if test.ifMatch != "" {
			request.Header.Set("If-Match", test.ifMatch)
		}
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusBadRequest {
			t.Errorf("%s %s: status=%d", test.method, test.path, response.Code)
		}
	}
}
