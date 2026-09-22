# Key-value reference solution

Run `./deploy.sh` from this directory in a fresh Linux sandbox with Docker,
k3d, kubectl, Go, curl, and GNU `timeout` installed. Set `DEPLOY_OUTPUT_DIR`
to a writable directory. The script creates a three-node K3s cluster,
imports `/seed/kv.jsonl`, and serves port 8080. On success it writes
`result.json` and `kubeconfig` to `DEPLOY_OUTPUT_DIR`; the Kubernetes API is
reachable on port 6443. The deployment stays running after the script exits.

Seed lines are JSON objects: `{"key":"example","value":"text"}`.
Duplicate keys use the last value in file order.

API bodies use JSON. `PUT /v1/kv/example` accepts `{"value":"text"}` and
returns `{"version":"42"}` with `ETag: "42"`. `GET` returns `value` and
`version`; `DELETE` returns 204. A conditional `PUT` sends `If-Match: "42"`
and returns 412 when the current version differs. `GET /healthz` checks
storage quorum.

The API uses linearizable etcd reads and Raft committed writes. Three etcd
members occupy distinct Kubernetes nodes. A node failure leaves a two-member
quorum; losing quorum makes storage operations return 503.

For local smoke tests, `KV_SEED_FILE=/path/to/kv.jsonl ./deploy.sh` overrides
the default seed path. `DEPLOY_HOST_IP` can override the detected sandbox
network address. Set `ETCD_TEST_ENDPOINTS` to a reachable etcd endpoint to run
the integration test with `go test ./...`.
