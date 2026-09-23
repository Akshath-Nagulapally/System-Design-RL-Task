package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const (
	etcdPrefix     = "kv:"
	requestTimeout = 8 * time.Second
)

type server struct {
	etcd *clientv3.Client
}

func main() {
	importMode := flag.Bool("import", false, "import newline-delimited JSON from stdin")
	flag.Parse()

	endpoints := strings.Split(os.Getenv("ETCD_ENDPOINTS"), ",")
	for index := range endpoints {
		endpoints[index] = strings.TrimSpace(endpoints[index])
	}
	if len(endpoints) == 0 || endpoints[0] == "" {
		endpoints = []string{"http://etcd:2379"}
	}

	etcd, err := clientv3.New(clientv3.Config{
		Endpoints:   endpoints,
		DialTimeout: 2 * time.Second,
	})
	if err != nil {
		log.Fatalf("create etcd client: %v", err)
	}
	if !*importMode {
		defer etcd.Close()
	}

	if *importMode {
		if err := importSeed(context.Background(), etcd, os.Stdin); err != nil {
			log.Fatalf("import seed: %v", err)
		}
		os.Exit(0)
		return
	}

	if err := run(server{etcd: etcd}); err != nil {
		log.Fatalf("HTTP server: %v", err)
	}
}
func run(api server) error {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", api.health)
	mux.HandleFunc("/v1/kv/", api.key)

	httpServer := &http.Server{
		Addr:              ":8080",
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	shutdown := make(chan os.Signal, 1)
	signal.Notify(shutdown, syscall.SIGINT, syscall.SIGTERM)
	serverError := make(chan error, 1)

	go func() {
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverError <- err
		}
	}()

	select {
	case err := <-serverError:
		return err
	case <-shutdown:
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return httpServer.Shutdown(ctx)
	}
}

func (s server) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), time.Second)
	defer cancel()
	_, err := s.etcd.Get(ctx, "__health__", clientv3.WithLimit(1))
	if err != nil {
		http.Error(w, "not ready", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s server) key(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.URL.Path, "/v1/kv/")
	if !validKey(key) {
		http.Error(w, "invalid key", http.StatusBadRequest)
		return
	}

	switch r.Method {
	case http.MethodGet:
		s.get(w, r, key)
	case http.MethodPut:
		s.put(w, r, key)
	case http.MethodDelete:
		s.delete(w, r, key)
	default:
		w.Header().Set("Allow", "GET, PUT, DELETE")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s server) get(w http.ResponseWriter, r *http.Request, key string) {
	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	response, err := s.etcd.Get(ctx, etcdPrefix+key)
	if err != nil {
		http.Error(w, "datastore unavailable", http.StatusServiceUnavailable)
		return
	}
	if len(response.Kvs) == 0 {
		http.Error(w, "key not found", http.StatusNotFound)
		return
	}

	kv := response.Kvs[0]
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("ETag", etag(kv.ModRevision))
	_ = json.NewEncoder(w).Encode(getResponse{
		Value:   string(kv.Value),
		Version: strconv.FormatInt(kv.ModRevision, 10),
	})
}

func (s server) put(w http.ResponseWriter, r *http.Request, key string) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 8192))
	if err != nil {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}
	var payload putRequest
	if err := json.Unmarshal(body, &payload); err != nil ||
		!utf8.ValidString(payload.Value) || len(payload.Value) > 4096 {
		http.Error(w, "value must be a UTF-8 string of at most 4096 bytes", http.StatusBadRequest)
		return
	}

	conditionRevision, conditional, err := parseIfMatch(r.Header.Get("If-Match"))
	if err != nil {
		http.Error(w, "invalid If-Match", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	etcdKey := etcdPrefix + key
	put := clientv3.OpPut(etcdKey, payload.Value)

	if conditional {
		condition := clientv3.Compare(clientv3.ModRevision(etcdKey), "=", conditionRevision)
		response, err := s.etcd.Txn(ctx).If(condition).Then(put).Else(clientv3.OpGet(etcdKey)).Commit()
		if err != nil {
			http.Error(w, "datastore unavailable", http.StatusServiceUnavailable)
			return
		}
		if !response.Succeeded {
			http.Error(w, "version mismatch", http.StatusPreconditionFailed)
			return
		}
		writeVersion(w, response.Header.Revision)
		return
	}

	putResponse, putErr := s.etcd.Put(ctx, etcdKey, payload.Value)
	if putErr != nil {
		http.Error(w, "datastore unavailable", http.StatusServiceUnavailable)
		return
	}
	writeVersion(w, putResponse.Header.Revision)
}

func (s server) delete(w http.ResponseWriter, r *http.Request, key string) {
	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	if _, err := s.etcd.Delete(ctx, etcdPrefix+key); err != nil {
		http.Error(w, "datastore unavailable", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func writeVersion(w http.ResponseWriter, revision int64) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("ETag", etag(revision))
	_ = json.NewEncoder(w).Encode(versionResponse{
		Version: strconv.FormatInt(revision, 10),
	})
}

type putRequest struct {
	Value string `json:"value"`
}

type getResponse struct {
	Value   string `json:"value"`
	Version string `json:"version"`
}

type versionResponse struct {
	Version string `json:"version"`
}

func validKey(key string) bool {
	if len(key) < 1 || len(key) > 128 {
		return false
	}
	for _, char := range key {
		if !(char >= 'a' && char <= 'z' ||
			char >= 'A' && char <= 'Z' ||
			char >= '0' && char <= '9' ||
			char == '-' || char == '_') {
			return false
		}
	}
	return true
}

func parseIfMatch(header string) (int64, bool, error) {
	if header == "" {
		return 0, false, nil
	}
	if len(header) < 2 || header[0] != '"' || header[len(header)-1] != '"' {
		return 0, true, errors.New("ETag must be quoted")
	}
	version, err := strconv.ParseInt(header[1:len(header)-1], 10, 64)
	if err != nil || version < 1 {
		return 0, true, errors.New("invalid ETag version")
	}
	return version, true, nil
}

func etag(version int64) string {
	return fmt.Sprintf(`"%d"`, version)
}

func importSeed(ctx context.Context, etcd *clientv3.Client, input io.Reader) error {
	decoder := json.NewDecoder(input)
	count := 0
	var ops []clientv3.Op

	flush := func() error {
		if len(ops) == 0 {
			return nil
		}
		response, err := etcd.Txn(ctx).Then(ops...).Commit()
		if err != nil {
			return err
		}
		if !response.Succeeded {
			return errors.New("seed transaction failed")
		}
		ops = ops[:0]
		return nil
	}

	for {
		var record struct {
			Key   string `json:"key"`
			Value string `json:"value"`
		}
		if err := decoder.Decode(&record); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return err
		}
		if !validKey(record.Key) || !utf8.ValidString(record.Value) || len(record.Value) > 4096 {
			return fmt.Errorf("invalid record at index %d", count)
		}
		ops = append(ops, clientv3.OpPut(etcdPrefix+record.Key, record.Value))
		count++
		if len(ops) == 64 {
			if err := flush(); err != nil {
				return err
			}
		}
	}
	if err := flush(); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "imported %d keys\n", count)
	return nil
}
