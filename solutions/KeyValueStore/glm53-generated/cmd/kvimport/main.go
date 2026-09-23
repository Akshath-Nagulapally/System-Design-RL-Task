package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

type record struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: kvimport FILE")
		os.Exit(2)
	}
	client, err := clientv3.New(clientv3.Config{
		Endpoints:   splitEndpoints(os.Getenv("ETCD_ENDPOINTS")),
		DialTimeout: 3 * time.Second,
	})
	if err != nil {
		fatal(err)
	}
	defer client.Close()
	file, err := os.Open(os.Args[1])
	if err != nil {
		fatal(err)
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	var operations []clientv3.Op
	imported := 0
	commit := func() {
		if len(operations) == 0 {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		response, err := client.Txn(ctx).Then(operations...).Commit()
		cancel()
		if err != nil || !response.Succeeded {
			fatal(fmt.Errorf("import transaction failed: %w", err))
		}
		operations = operations[:0]
	}
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var item record
		if err := json.Unmarshal(line, &item); err != nil {
			fatal(err)
		}
		operations = append(operations, clientv3.OpPut("kv/"+item.Key, item.Value))
		imported++
		if len(operations) == 500 {
			commit()
		}
	}
	if err := scanner.Err(); err != nil {
		fatal(err)
	}
	commit()
	fmt.Printf("imported %d records\n", imported)
}

func splitEndpoints(value string) []string {
	if value == "" {
		return []string{"http://127.0.0.1:2379"}
	}
	endpoints := make([]string, 0, 4)
	for value != "" {
		index := 0
		for index < len(value) && value[index] != ',' {
			index++
		}
		if index == len(value) {
			endpoints = append(endpoints, value)
			break
		}
		if index != 0 {
			endpoints = append(endpoints, value[:index])
		}
		value = value[index+1:]
	}
	return endpoints
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
