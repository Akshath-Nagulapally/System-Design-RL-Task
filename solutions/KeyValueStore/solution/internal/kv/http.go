package kv

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	maxKeyBytes   = 128
	maxValueBytes = 4 * 1024
	maxBodyBytes  = 64 * 1024
)

type Handler struct {
	store   Store
	timeout time.Duration
}

func NewHandler(store Store, timeout time.Duration) http.Handler {
	return &Handler{store: store, timeout: timeout}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
		defer cancel()
		if err := h.store.Ready(ctx); err != nil {
			writeError(w, http.StatusServiceUnavailable, "storage unavailable")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
		return
	}

	if !strings.HasPrefix(r.URL.Path, "/v1/kv/") {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	key := strings.TrimPrefix(r.URL.Path, "/v1/kv/")
	if !ValidKey(key) {
		writeError(w, http.StatusBadRequest, "invalid key")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		h.get(ctx, w, key)
	case http.MethodPut:
		h.put(ctx, w, r, key)
	case http.MethodDelete:
		h.delete(ctx, w, key)
	default:
		w.Header().Set("Allow", "GET, PUT, DELETE")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func ValidKey(key string) bool {
	if len(key) == 0 || len(key) > maxKeyBytes {
		return false
	}
	for i := 0; i < len(key); i++ {
		b := key[i]
		if !((b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') ||
			(b >= '0' && b <= '9') || b == '-' || b == '_' || b == '.' || b == '~') {
			return false
		}
	}
	return true
}

func (h *Handler) get(ctx context.Context, w http.ResponseWriter, key string) {
	value, version, found, err := h.store.Get(ctx, key)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "storage unavailable")
		return
	}
	if !found {
		writeError(w, http.StatusNotFound, "key not found")
		return
	}
	versionText := strconv.FormatInt(version, 10)
	w.Header().Set("ETag", fmt.Sprintf("%q", versionText))
	writeJSON(w, http.StatusOK, map[string]string{"value": value, "version": versionText})
}

func (h *Handler) put(ctx context.Context, w http.ResponseWriter, r *http.Request, key string) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	defer r.Body.Close()
	var body struct {
		Value *string `json:"value"`
	}
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil || body.Value == nil {
		writeError(w, http.StatusBadRequest, "expected JSON object with value string")
		return
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "expected one JSON object")
		return
	}
	if !utf8.ValidString(*body.Value) || len(*body.Value) > maxValueBytes {
		writeError(w, http.StatusBadRequest, "value must be UTF-8 and at most 4096 bytes")
		return
	}
	expected, err := parseIfMatch(r.Header.Get("If-Match"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid If-Match version")
		return
	}
	version, matched, err := h.store.Put(ctx, key, *body.Value, expected)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "storage unavailable")
		return
	}
	if !matched {
		writeError(w, http.StatusPreconditionFailed, "version mismatch")
		return
	}
	versionText := strconv.FormatInt(version, 10)
	w.Header().Set("ETag", fmt.Sprintf("%q", versionText))
	writeJSON(w, http.StatusOK, map[string]string{"version": versionText})
}

func parseIfMatch(raw string) (*int64, error) {
	if raw == "" {
		return nil, nil
	}
	if strings.HasPrefix(raw, "\"") && strings.HasSuffix(raw, "\"") && len(raw) >= 2 {
		raw = raw[1 : len(raw)-1]
	}
	version, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || version <= 0 {
		return nil, fmt.Errorf("invalid version")
	}
	return &version, nil
}

func (h *Handler) delete(ctx context.Context, w http.ResponseWriter, key string) {
	if err := h.store.Delete(ctx, key); err != nil {
		writeError(w, http.StatusServiceUnavailable, "storage unavailable")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func writeJSON(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, code int, message string) {
	writeJSON(w, code, map[string]string{"error": message})
}
