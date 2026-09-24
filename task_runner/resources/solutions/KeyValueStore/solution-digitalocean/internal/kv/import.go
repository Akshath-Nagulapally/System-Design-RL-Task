package kv

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"unicode/utf8"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const importBatchSize = 64

// ImportSeed accepts JSONL records of the form {"key":"...","value":"..."}.
// Batches are committed in file order, so duplicate keys have last-line-wins
// semantics. A retry of the same batch is safe before public traffic starts.
func ImportSeed(ctx context.Context, client *clientv3.Client, path string) (int, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 16*1024), 64*1024)
	var batch []clientv3.Op
	lineNumber := 0
	records := 0
	for scanner.Scan() {
		lineNumber++
		line := scanner.Bytes()
		if !utf8.Valid(line) {
			return records, fmt.Errorf("seed line %d: invalid UTF-8", lineNumber)
		}
		var record struct {
			Key   string  `json:"key"`
			Value *string `json:"value"`
		}
		if err := json.Unmarshal(line, &record); err != nil {
			return records, fmt.Errorf("seed line %d: %w", lineNumber, err)
		}
		if !ValidKey(record.Key) {
			return records, fmt.Errorf("seed line %d: invalid key", lineNumber)
		}
		if record.Value == nil || !utf8.ValidString(*record.Value) || len(*record.Value) > maxValueBytes {
			return records, fmt.Errorf("seed line %d: invalid value", lineNumber)
		}
		batch = append(batch, clientv3.OpPut(dataPrefix+record.Key, *record.Value))
		records++
		if len(batch) == importBatchSize {
			if err := putBatch(ctx, client, batch); err != nil {
				return records, fmt.Errorf("seed batch ending at line %d: %w", lineNumber, err)
			}
			batch = batch[:0]
		}
	}
	if err := scanner.Err(); err != nil {
		return records, err
	}
	if len(batch) > 0 {
		if err := putBatch(ctx, client, batch); err != nil {
			return records, err
		}
	}
	return records, nil
}

func putBatch(ctx context.Context, client *clientv3.Client, batch []clientv3.Op) error {
	_, err := client.Txn(ctx).Then(batch...).Commit()
	return err
}
