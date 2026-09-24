# Distributed key-value store on K3s

Work in `/app`, starting from an empty project. Build and deploy a key-value
service on K3s running on DigitalOcean Droplets. No UI is required.
Use this fixed stack:

- Implement the HTTP API and seed importer in Go 1.25.1.
- Use K3s `v1.35.5-k3s1` on the Droplets. The supplied kubectl is v1.35.5.
- Use etcd v3.6.14 as the application key-value datastore, with the
  Go etcd v3 client at v3.6.14. Do not use K3s's internal datastore for
  application data.

You may choose the service topology, replication layout, Go libraries other
than the etcd client, and Kubernetes manifests, provided they meet the
requirements below. Use `/app/deploy.sh` to orchestrate deployment.

Your submission must include all source files, manifests, and setup steps
needed for a fresh deployment. The deployment contract and resource budget
appended below specify the infrastructure and handoff requirements.

## Deployment
- Deployment must create the system, import the supplied read-only
  `/seed/kv.jsonl`, and expose the HTTP API within 15 minutes.
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
- Acknowledged writes and deletes survive the loss of one machine.
- Successful responses during degraded operation must satisfy the same
  correctness rules.

## Evaluation
Tests cover seed import, API edge cases, concurrent operations, hot keys,
sustained and overload traffic, and machine loss.
The grader records correctness, availability, throughput, and latency.

## Deployment handoff

On success, `deploy.sh` must keep the deployment running and write
`$DEPLOY_OUTPUT_DIR/result.json`. Example:

```json
{
  "endpoints": [
    {"name": "api", "scheme": "http", "url": "http://example.invalid"},
    {"name": "kubernetes", "scheme": "https", "url": "https://example.invalid:6443"}
  ],
  "artifacts": [
    {"name": "kubeconfig", "path": "kubeconfig"}
  ]
}
```

List only the endpoints and artifacts this task uses. Endpoint URLs must be
reachable from the runner. Artifact paths are relative to `DEPLOY_OUTPUT_DIR`;
place the files there.

On failure, exit with a nonzero status.
