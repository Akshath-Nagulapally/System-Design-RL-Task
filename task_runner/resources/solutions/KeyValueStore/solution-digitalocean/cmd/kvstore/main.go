package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"kvreference/internal/kv"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	endpoints := kv.Endpoints(os.Getenv("ETCD_ENDPOINTS"))
	if len(endpoints) == 0 {
		endpoints = []string{"127.0.0.1:2379"}
	}
	store, err := kv.NewEtcdStore(endpoints)
	if err != nil {
		return fmt.Errorf("connect to etcd: %w", err)
	}
	defer store.Close()

	if len(os.Args) >= 2 && os.Args[1] == "import" {
		if len(os.Args) != 3 {
			return errors.New("usage: kvstore import /seed/kv.jsonl")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		count, err := kv.ImportSeed(ctx, store.Client(), os.Args[2])
		if err != nil {
			return fmt.Errorf("import seed after %d records: %w", count, err)
		}
		log.Printf("imported %d seed records", count)
		return nil
	}
	if len(os.Args) > 1 {
		return errors.New("usage: kvstore [import /seed/kv.jsonl]")
	}

	server := &http.Server{
		Addr:              ":8080",
		Handler:           kv.NewHandler(store, 2*time.Second),
		ReadHeaderTimeout: 3 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stop)
	go func() {
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}()
	log.Printf("listening on %s with %d etcd endpoints", server.Addr, len(endpoints))
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
