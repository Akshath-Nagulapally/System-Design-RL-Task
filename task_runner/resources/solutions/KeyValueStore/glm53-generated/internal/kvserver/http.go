package kvserver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
)

var keyPattern = regexp.MustCompile(`^[A-Za-z0-9._~-]+$`)

type handler struct {
	client *Client
}

func New(client *Client) http.Handler {
	return &handler{client: client}
}

func (handler *handler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path == "/healthz" {
		if request.Method != http.MethodGet {
			handler.error(writer, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		handler.respond(writer, http.StatusOK, map[string]string{"status": "ok"})
		return
	}
	if !strings.HasPrefix(request.URL.Path, "/v1/kv/") {
		handler.error(writer, http.StatusNotFound, "not found")
		return
	}
	key := request.URL.Path[len("/v1/kv/"):]
	if !keyPattern.MatchString(key) || len(key) < 1 || len(key) > 128 {
		handler.error(writer, http.StatusBadRequest, "invalid key")
		return
	}
	switch request.Method {
	case http.MethodGet:
		handler.get(writer, request, key)
	case http.MethodPut:
		handler.put(writer, request, key)
	case http.MethodDelete:
		handler.delete(writer, request, key)
	default:
		handler.error(writer, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func (handler *handler) get(writer http.ResponseWriter, request *http.Request, key string) {
	value, version, found, err := handler.client.Get(request.Context(), key)
	if err != nil {
		handler.storageError(writer, err)
		return
	}
	if !found {
		handler.error(writer, http.StatusNotFound, "key not found")
		return
	}
	writer.Header().Set("ETag", `"`+version+`"`)
	handler.respond(writer, http.StatusOK, map[string]string{"value": value, "version": version})
}

func (handler *handler) put(writer http.ResponseWriter, request *http.Request, key string) {
	var payload struct {
		Value *string `json:"value"`
	}
	decoder := json.NewDecoder(io.LimitReader(request.Body, 64*1024))
	if err := decoder.Decode(&payload); err != nil || payload.Value == nil {
		handler.error(writer, http.StatusBadRequest, "value must be a UTF-8 string")
		return
	}
	var extra interface{}
	if err := decoder.Decode(&extra); err != io.EOF {
		handler.error(writer, http.StatusBadRequest, "invalid JSON body")
		return
	}
	value := *payload.Value
	if len(value) > 4096 {
		handler.error(writer, http.StatusBadRequest, "value too large")
		return
	}
	ifMatch := strings.TrimSpace(request.Header.Get("If-Match"))
	if ifMatch != "" && (len(ifMatch) < 2 || !strings.HasPrefix(ifMatch, `"`) || !strings.HasSuffix(ifMatch, `"`)) {
		handler.error(writer, http.StatusBadRequest, "invalid If-Match")
		return
	}
	if ifMatch != "" {
		ifMatch = ifMatch[1 : len(ifMatch)-1]
		if _, err := strconv.ParseInt(ifMatch, 10, 64); err != nil {
			handler.error(writer, http.StatusBadRequest, "invalid If-Match")
			return
		}
	}
	version, updated, err := handler.client.Put(request.Context(), key, value, ifMatch)
	if err != nil {
		if errors.Is(err, errInvalidIfMatch) {
			handler.error(writer, http.StatusBadRequest, "invalid If-Match")
			return
		}
		handler.storageError(writer, err)
		return
	}
	if !updated {
		handler.error(writer, http.StatusPreconditionFailed, "version mismatch")
		return
	}
	writer.Header().Set("ETag", `"`+version+`"`)
	handler.respond(writer, http.StatusOK, map[string]string{"version": version})
}

func (handler *handler) delete(writer http.ResponseWriter, request *http.Request, key string) {
	deleted, err := handler.client.Delete(request.Context(), key)
	if err != nil {
		handler.storageError(writer, err)
		return
	}
	if !deleted {
		handler.error(writer, http.StatusNotFound, "key not found")
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}

func (handler *handler) respond(writer http.ResponseWriter, status int, body interface{}) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(body)
}

func (handler *handler) error(writer http.ResponseWriter, status int, message string) {
	handler.respond(writer, status, map[string]string{"error": message})
}

func (handler *handler) storageError(writer http.ResponseWriter, err error) {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		handler.error(writer, http.StatusServiceUnavailable, "storage unavailable")
		return
	}
	message := err.Error()
	if strings.Contains(message, "context deadline") || strings.Contains(message, "connection refused") ||
		strings.Contains(message, "unavailable") || strings.Contains(message, "connection reset") {
		handler.error(writer, http.StatusServiceUnavailable, "storage unavailable")
		return
	}
	handler.error(writer, http.StatusInternalServerError, "storage error")
}
