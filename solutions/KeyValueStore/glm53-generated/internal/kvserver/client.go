package kvserver

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const namespacePrefix = "kv/"

var errInvalidIfMatch = errors.New("invalid If-Match")

type Client struct {
	etcd *clientv3.Client
}

func NewClient(endpoint string) *Client {
	etcd, err := clientv3.New(clientv3.Config{
		Endpoints:   parseEndpoints(endpoint),
		DialTimeout: 3 * time.Second,
	})
	if err != nil {
		panic(err)
	}
	return &Client{etcd: etcd}
}

func parseEndpoints(endpoint string) []string {
	if endpoint == "" {
		return []string{"http://127.0.0.1:2379"}
	}
	endpoints := make([]string, 0, 4)
	for item := endpoint; item != ""; {
		next := ""
		if index := strings.IndexByte(item, ','); index >= 0 {
			next, item = item[index+1:], item[:index]
		}
		item = strings.TrimSpace(item)
		if item != "" {
			endpoints = append(endpoints, item)
		}
		item = next
	}
	return endpoints
}

func (client *Client) WaitForReady(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var lastErr error
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		for _, endpoint := range client.etcd.Endpoints() {
			_, lastErr = client.etcd.Status(ctx, endpoint)
			if lastErr == nil {
				cancel()
				return nil
			}
		}
		cancel()
		if !time.Now().Before(deadline) {
			if lastErr == nil {
				lastErr = errors.New("no etcd endpoints")
			}
			return lastErr
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func (client *Client) Get(ctx context.Context, key string) (string, string, bool, error) {
	response, err := client.etcd.Get(ctx, namespacePrefix+key, clientv3.WithLimit(1))
	if err != nil {
		return "", "", false, fmt.Errorf("get: %w", err)
	}
	if len(response.Kvs) == 0 {
		return "", "", false, nil
	}
	return string(response.Kvs[0].Value), strconv.FormatInt(response.Kvs[0].ModRevision, 10), true, nil
}

func (client *Client) Put(ctx context.Context, key, value, ifMatch string) (string, bool, error) {
	revision := int64(0)
	conditional := ifMatch != ""
	if conditional {
		var err error
		revision, err = strconv.ParseInt(ifMatch, 10, 64)
		if err != nil || ifMatch != strconv.FormatInt(revision, 10) {
			return "", false, errInvalidIfMatch
		}
	}
	transaction := client.etcd.Txn(ctx)
	if conditional {
		transaction = transaction.If(clientv3.Compare(clientv3.ModRevision(namespacePrefix+key), "=", revision))
	}
	response, err := transaction.Then(clientv3.OpPut(namespacePrefix+key, value)).Commit()
	if err != nil {
		return "", false, fmt.Errorf("put: %w", err)
	}
	if conditional && !response.Succeeded {
		return "", false, nil
	}
	return strconv.FormatInt(response.Header.Revision, 10), true, nil
}

func (client *Client) Delete(ctx context.Context, key string) (bool, error) {
	response, err := client.etcd.Delete(ctx, namespacePrefix+key)
	if err != nil {
		return false, fmt.Errorf("delete: %w", err)
	}
	return response.Deleted > 0, nil
}

func (client *Client) Close() error {
	return client.etcd.Close()
}
