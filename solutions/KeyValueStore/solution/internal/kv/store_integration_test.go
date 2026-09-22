package kv

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"
)

// Set ETCD_TEST_ENDPOINTS to a running etcd cluster to exercise real Raft
// transactions and linearizable reads. Normal unit tests require no daemon.
func TestEtcdConditionalWrites(t *testing.T) {
	raw := os.Getenv("ETCD_TEST_ENDPOINTS")
	if raw == "" {
		t.Skip("ETCD_TEST_ENDPOINTS is not set")
	}
	store, err := NewEtcdStore(Endpoints(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	key := fmt.Sprintf("test-%d", time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	defer store.Delete(context.Background(), key)

	firstVersion, ok, err := store.Put(ctx, key, "initial", nil)
	if err != nil || !ok {
		t.Fatalf("initial PUT: version=%d ok=%v err=%v", firstVersion, ok, err)
	}
	const clients = 16
	var wg sync.WaitGroup
	success := make(chan string, clients)
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			value := fmt.Sprintf("writer-%d", i)
			_, matched, err := store.Put(ctx, key, value, &firstVersion)
			if err == nil && matched {
				success <- value
			} else if err != nil {
				t.Errorf("writer %d: %v", i, err)
			}
		}(i)
	}
	wg.Wait()
	close(success)
	winner := ""
	for value := range success {
		if winner != "" {
			t.Fatalf("two writers committed against one version")
		}
		winner = value
	}
	if winner == "" {
		t.Fatal("no conditional writer committed")
	}
	value, version, found, err := store.Get(ctx, key)
	if err != nil || !found || value != winner || version <= firstVersion {
		t.Fatalf("GET after race: value=%q version=%d found=%v err=%v", value, version, found, err)
	}
	if err := store.Delete(ctx, key); err != nil {
		t.Fatal(err)
	}
	_, _, found, err = store.Get(ctx, key)
	if err != nil || found {
		t.Fatalf("GET after DELETE: found=%v err=%v", found, err)
	}
}
