# Distributed key-value store on K3s

Work in `/app`, starting from an empty project. Build and deploy a key-value
service on a local Kubernetes cluster. No cloud services or UI are required.
You may choose the implementation language, storage design, and topology.

Your submission must include an executable `/app/deploy.sh` and all source
files, manifests, and setup steps needed in a fresh sandbox. The script must
create a local K3s cluster; the Harbor environment provides a privileged
Docker daemon for this purpose.

## Deployment
- The evaluation sandbox has 6 vCPU, 8 GiB RAM, and at most 3 Kubernetes
  nodes. These limits include the cluster and all workloads.
- `deploy.sh` must create and deploy the system, import the supplied read-only
  `/seed/kv.jsonl`, and expose HTTP on port 8080 within 15 minutes.
- The same script must work in a fresh sandbox. A failed clean deployment
  receives a score of 0.

## API
- `PUT /v1/kv/{key}` accepts JSON `{"value":"..."}` containing a UTF-8
  string. On success it returns JSON `{"version":"..."}` and an `ETag`
  header containing the quoted version.
- `GET /v1/kv/{key}` returns JSON `{"value":"...","version":"..."}` and
  the corresponding `ETag`, or HTTP 404 when the key is absent.
- `DELETE /v1/kv/{key}` removes the key and returns HTTP 204 on success.
- `GET /healthz` returns HTTP 200 only when the service is ready.
- PUT supports a quoted version in `If-Match` for a conditional update; a
  version mismatch returns HTTP 412. Keys are 1–128 URL-safe characters;
  values are at most 4 KiB.

## Guarantees
- Successful operations on each key are linearizable.
- Acknowledged writes and deletes survive the loss of one worker node.
- During a partition, requests may fail with 503; successful responses
  must satisfy the same correctness rules.
- After recovery, the service must retain all acknowledged mutations.

## Evaluation
Tests cover seed import, API edge cases, concurrent operations, hot keys,
sustained and overload traffic, worker loss, network partition, and recovery.
The grader records correctness, availability, throughput, and latency.

## Deployment handoff

The runner sets `DEPLOY_OUTPUT_DIR` to a writable directory.
The submission is mounted at `/app`; `/seed/kv.jsonl` is read-only. The
privileged Docker sandbox includes Docker, Go, a C compiler, OpenRC, k3d,
kubectl, and jq. A fresh sandbox starts with no existing Kubernetes cluster.

On success, `deploy.sh` must keep the deployment running and write
`$DEPLOY_OUTPUT_DIR/result.json`. Example:

```json
{
  "endpoints": [
    {"name": "api", "scheme": "http", "port": 8080},
    {"name": "kubernetes", "scheme": "https", "port": 6443}
  ],
  "artifacts": [
    {"name": "kubeconfig", "path": "kubeconfig"}
  ]
}
```

List only the endpoints and artifacts this task uses. Endpoint ports must be
reachable on the sandbox's network interface. Artifact paths are relative to
`DEPLOY_OUTPUT_DIR`; place the files there.

On failure, exit with a nonzero status.
