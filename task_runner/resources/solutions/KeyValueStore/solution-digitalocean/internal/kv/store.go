package kv

import (
	"context"
	"strings"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const dataPrefix = "data/"

// Store exposes the operations needed by the HTTP API. Every read uses etcd's
// default linearizable mode; writes and conditional writes commit through Raft.
type Store interface {
	Get(context.Context, string) (string, int64, bool, error)
	Put(context.Context, string, string, *int64) (int64, bool, error)
	Delete(context.Context, string) error
	Ready(context.Context) error
}

type EtcdStore struct {
	client *clientv3.Client
}

func NewEtcdStore(endpoints []string) (*EtcdStore, error) {
	client, err := clientv3.New(clientv3.Config{
		Endpoints:   endpoints,
		DialTimeout: 3 * time.Second,
	})
	if err != nil {
		return nil, err
	}
	return &EtcdStore{client: client}, nil
}

func (s *EtcdStore) Close() error { return s.client.Close() }

func (s *EtcdStore) Client() *clientv3.Client { return s.client }

func (s *EtcdStore) Get(ctx context.Context, key string) (string, int64, bool, error) {
	resp, err := s.client.Get(ctx, dataPrefix+key)
	if err != nil {
		return "", 0, false, err
	}
	if len(resp.Kvs) == 0 {
		return "", 0, false, nil
	}
	item := resp.Kvs[0]
	return string(item.Value), item.ModRevision, true, nil
}

func (s *EtcdStore) Put(ctx context.Context, key, value string, expected *int64) (int64, bool, error) {
	name := dataPrefix + key
	if expected == nil {
		resp, err := s.client.Put(ctx, name, value)
		if err != nil {
			return 0, false, err
		}
		return resp.Header.Revision, true, nil
	}
	resp, err := s.client.Txn(ctx).
		If(clientv3.Compare(clientv3.ModRevision(name), "=", *expected)).
		Then(clientv3.OpPut(name, value)).
		Commit()
	if err != nil {
		return 0, false, err
	}
	if !resp.Succeeded {
		return 0, false, nil
	}
	return resp.Header.Revision, true, nil
}

func (s *EtcdStore) Delete(ctx context.Context, key string) error {
	_, err := s.client.Delete(ctx, dataPrefix+key)
	return err
}

func (s *EtcdStore) Ready(ctx context.Context) error {
	// A local TCP check could return healthy on a minority partition. A default
	// linearizable read proves this process can currently reach quorum.
	_, err := s.client.Get(ctx, "_readiness_probe")
	return err
}

func Endpoints(raw string) []string {
	var out []string
	for _, endpoint := range strings.Split(raw, ",") {
		endpoint = strings.TrimSpace(endpoint)
		if endpoint != "" {
			out = append(out, endpoint)
		}
	}
	return out
}
