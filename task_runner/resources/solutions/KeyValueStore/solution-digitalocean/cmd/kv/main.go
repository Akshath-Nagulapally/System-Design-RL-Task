package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const (
	maxValueBytes = 4096
	keyPrefix     = "/kv/"
)

var keyPattern = regexp.MustCompile(`^[A-Za-z0-9._~-]{1,128}$`)
var versionPattern = regexp.MustCompile(`^"([1-9][0-9]*)"$`)

type service struct {
	client *clientv3.Client
}

type valueRequest struct {
	Value *string `json:"value"`
}

type valueResponse struct {
	Value   string `json:"value"`
	Version string `json:"version"`
}

func main() {
	endpoints := strings.Split(os.Getenv("ETCD_ENDPOINTS"), ",")
	if len(endpoints) == 0 || endpoints[0] == "" {
		log.Fatal("ETCD_ENDPOINTS must be set")
	}
	client, err := clientv3.New(clientv3.Config{
		Endpoints:            endpoints,
		DialTimeout:          3 * time.Second,
		DialKeepAliveTime:    10 * time.Second,
		DialKeepAliveTimeout: 3 * time.Second,
	})
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()

	if len(os.Args) > 1 && os.Args[1] == "import" {
		if err := importSeed(client, os.Stdin); err != nil {
			log.Fatal(err)
		}
		return
	}
	if len(os.Args) > 1 {
		log.Fatalf("unknown command: %s", os.Args[1])
	}

	handler := &service{client: client}
	mux := http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch {
		case request.URL.Path == "/healthz":
			handler.health(writer, request)
		case strings.HasPrefix(request.URL.Path, "/v1/kv/"):
			handler.kv(writer, request)
		default:
			http.NotFound(writer, request)
		}
	})
	server := &http.Server{
		Addr:              ":8080",
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       90 * time.Second,
	}
	log.Fatal(server.ListenAndServe())
}

func (service *service) health(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	context, cancel := context.WithTimeout(request.Context(), 2*time.Second)
	defer cancel()
	_, err := service.client.Get(context, "/kv-healthcheck", clientv3.WithLimit(1))
	if err != nil {
		http.Error(writer, "datastore unavailable", http.StatusServiceUnavailable)
		return
	}
	writer.WriteHeader(http.StatusOK)
}

func (service *service) kv(writer http.ResponseWriter, request *http.Request) {
	key := strings.TrimPrefix(request.URL.Path, "/v1/kv/")
	if !keyPattern.MatchString(key) {
		http.Error(writer, "invalid key", http.StatusBadRequest)
		return
	}
	context, cancel := context.WithTimeout(request.Context(), 5*time.Second)
	defer cancel()
	storeKey := keyPrefix + key

	switch request.Method {
	case http.MethodGet:
		response, err := service.client.Get(context, storeKey)
		if err != nil {
			unavailable(writer, err)
			return
		}
		if len(response.Kvs) == 0 {
			http.Error(writer, "not found", http.StatusNotFound)
			return
		}
		version := strconv.FormatInt(response.Kvs[0].ModRevision, 10)
		writer.Header().Set("ETag", strconv.Quote(version))
		writeJSON(writer, http.StatusOK, valueResponse{Value: string(response.Kvs[0].Value), Version: version})
	case http.MethodPut:
		service.put(writer, request, context, storeKey)
	case http.MethodDelete:
		if _, err := service.client.Delete(context, storeKey); err != nil {
			unavailable(writer, err)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	default:
		writer.Header().Set("Allow", "GET, PUT, DELETE")
		writer.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (service *service) put(writer http.ResponseWriter, request *http.Request, context context.Context, storeKey string) {
	body, err := io.ReadAll(http.MaxBytesReader(writer, request.Body, 32768))
	if err != nil || !utf8.Valid(body) {
		http.Error(writer, "invalid JSON body", http.StatusBadRequest)
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var payload valueRequest
	if err := decoder.Decode(&payload); err != nil {
		http.Error(writer, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if decoder.Decode(new(any)) != io.EOF || payload.Value == nil || len(*payload.Value) > maxValueBytes {
		http.Error(writer, "invalid JSON body or value too long", http.StatusBadRequest)
		return
	}

	var revision int64
	if values, exists := request.Header[http.CanonicalHeaderKey("If-Match")]; exists {
		if len(values) != 1 {
			http.Error(writer, "invalid If-Match", http.StatusBadRequest)
			return
		}
		parts := versionPattern.FindStringSubmatch(values[0])
		if parts == nil {
			http.Error(writer, "invalid If-Match", http.StatusBadRequest)
			return
		}
		matchRevision, err := strconv.ParseInt(parts[1], 10, 64)
		if err != nil {
			http.Error(writer, "invalid If-Match", http.StatusBadRequest)
			return
		}
		response, err := service.client.Txn(context).
			If(clientv3.Compare(clientv3.ModRevision(storeKey), "=", matchRevision)).
			Then(clientv3.OpPut(storeKey, *payload.Value)).Commit()
		if err != nil {
			unavailable(writer, err)
			return
		}
		if !response.Succeeded {
			http.Error(writer, "version mismatch", http.StatusPreconditionFailed)
			return
		}
		revision = response.Header.Revision
	} else {
		response, err := service.client.Put(context, storeKey, *payload.Value)
		if err != nil {
			unavailable(writer, err)
			return
		}
		revision = response.Header.Revision
	}
	version := strconv.FormatInt(revision, 10)
	writer.Header().Set("ETag", strconv.Quote(version))
	writeJSON(writer, http.StatusOK, struct {
		Version string `json:"version"`
	}{Version: version})
}

func writeJSON(writer http.ResponseWriter, status int, payload any) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.WriteHeader(status)
	if err := json.NewEncoder(writer).Encode(payload); err != nil {
		log.Printf("writing response: %v", err)
	}
}

func unavailable(writer http.ResponseWriter, err error) {
	log.Printf("datastore operation failed: %v", err)
	http.Error(writer, "datastore unavailable", http.StatusServiceUnavailable)
}

func importSeed(client *clientv3.Client, source io.Reader) error {
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 64*1024), 128*1024)
	operations := make([]clientv3.Op, 0, 64)
	pending := make(map[string]int, 64)
	line := 0
	flush := func() error {
		if len(operations) == 0 {
			return nil
		}
		context, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_, err := client.Txn(context).Then(operations...).Commit()
		operations = operations[:0]
		clear(pending)
		return err
	}
	for scanner.Scan() {
		line++
		text := scanner.Bytes()
		if len(bytes.TrimSpace(text)) == 0 {
			continue
		}
		if !utf8.Valid(text) {
			return fmt.Errorf("seed line %d is not UTF-8", line)
		}
		var item struct {
			Key   string `json:"key"`
			Value string `json:"value"`
		}
		if err := json.Unmarshal(text, &item); err != nil {
			return fmt.Errorf("seed line %d: %w", line, err)
		}
		if !keyPattern.MatchString(item.Key) || len(item.Value) > maxValueBytes {
			return fmt.Errorf("seed line %d has invalid key or value", line)
		}
		operation := clientv3.OpPut(keyPrefix+item.Key, item.Value)
		if position, exists := pending[item.Key]; exists {
			operations[position] = operation
		} else {
			pending[item.Key] = len(operations)
			operations = append(operations, operation)
		}
		if len(operations) == cap(operations) {
			if err := flush(); err != nil {
				return fmt.Errorf("seed line %d: %w", line, err)
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if err := flush(); err != nil {
		return err
	}
	if line == 0 {
		return errors.New("seed file is empty")
	}
	log.Printf("imported %d seed lines", line)
	return nil
}
