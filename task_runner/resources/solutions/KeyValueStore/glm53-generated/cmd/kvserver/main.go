package main

import (
	"log"
	"net/http"
	"os"
	"time"

	"kvstore/internal/kvserver"
)

func main() {
	client := kvserver.NewClient(os.Getenv("ETCD_ENDPOINTS"))
	if err := client.WaitForReady(30 * time.Second); err != nil {
		log.Fatalf("etcd readiness: %v", err)
	}
	server := &http.Server{
		Addr:              ":8080",
		Handler:           kvserver.New(client),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Println("kv server listening on :8080")
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
