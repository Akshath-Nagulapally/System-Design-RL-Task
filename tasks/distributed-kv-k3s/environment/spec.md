# Distributed Key-Value Store

Build and deploy a key-value service on a local Kubernetes cluster. No cloud
services or UI are required. You may choose the implementation and topology.

Spinning up kubernetes cluster should also be included within our script. The technology should be standardized: K3s.

## Deployment
- The evaluation sandbox has 6 vCPU, 8 GiB RAM, and at most 3 Kubernetes
  nodes. These limits include the cluster and all workloads.
- `deploy.sh` must create/deploy the system, import the supplied read-only
  `/seed/kv.jsonl`, and expose HTTP on port 8080 within 15 minutes. It must also be present in the root directory of the submission.
- The same script must work in a fresh sandbox. A failed clean deployment
  receives a score of 0.

## API
- `PUT /v1/kv/{key}` — store a UTF-8 value; return its version.
- `GET /v1/kv/{key}` — return value and version, or 404.
- `DELETE /v1/kv/{key}` — remove the key.
- `GET /healthz` — report readiness.
- PUT supports `If-Match` for a conditional update; a version mismatch
  returns 412. Keys are 1–128 URL-safe characters; values are at most 4 KiB.

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
