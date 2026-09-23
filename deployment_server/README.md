# One-shot deployment server

This server accepts one repository as a gzip tarball over `POST /deploy`. It
runs the repository's root `deploy.sh` in a 6 vCPU, 8 GiB Docker-in-Docker
sandbox. A second upload receives HTTP 409 with `{"error":"one already
provided"}`, including while the first deployment is running. The claim is
stored in `DEPLOY_STATE_DIR` and survives server restarts. A failed deployment
receives `{"error":"error deployment unsuccessful"}`.

`deploy.sh` must write `$DEPLOY_OUTPUT_DIR/result.json` after successful
deployment. The server forwards every declared port through a small TCP relay
and returns public URLs by endpoint name. Files declared as artifacts are
returned in the same JSON response. If an artifact is named `kubeconfig` and
an endpoint is named `kubernetes`, the server updates its API address while
preserving the original TLS certificate name.

```json
{
  "endpoints": [
    {"name": "api", "scheme": "http", "port": 8080},
    {"name": "kubernetes", "scheme": "https", "port": 6443}
  ],
  "artifacts": [{"name": "kubeconfig", "path": "kubeconfig"}]
}
```

Endpoint ports must listen on the sandbox interface and remain available after
`deploy.sh` exits. Artifact paths are relative to `DEPLOY_OUTPUT_DIR`.

## Run

Requires a running Linux Docker engine or Docker Desktop, Python 3.11+, and
enough Docker memory for the 8 GiB sandbox. Build the two images:

```sh
docker build -f deployment_server/Dockerfile.sandbox -t deployment-sandbox:latest .
docker build -f deployment_server/Dockerfile.proxy -t deployment-proxy:latest .
```

For the KeyValueStore task, mount an existing JSONL file at `/seed/kv.jsonl`
with `DEPLOY_INPUT_MOUNTS`. The same setting can mount other task inputs at
other paths. Set `DEPLOY_PUBLIC_HOST` to an address the grader can reach. The default is
`127.0.0.1`, which keeps relays local. Set `DEPLOY_BIND_HOST` and
`DEPLOY_SERVER_TOKEN` if the upload API must be remotely accessible.

```sh
DEPLOY_INPUT_MOUNTS='[{"source":"/absolute/path/kv.jsonl","target":"/seed/kv.jsonl"}]' \
  uv run python -m deployment_server.server
```

In another terminal, upload the repository folder and save the response:

```sh
tar -czf /tmp/submission.tar.gz -C tasks/solutions/KeyValueStore/solution .
curl -fsS -H 'Content-Type: application/gzip' \
  --data-binary @/tmp/submission.tar.gz \
  http://127.0.0.1:8000/deploy > /tmp/deployment.json
```

If the upload connection drops, `GET /result` returns `202` while the job is
running and the saved response when it finishes. The response has
`endpoints.api`, `endpoints.kubernetes`, and
`artifacts.kubeconfig` for this task. For example:

```sh
python3 -c 'import json; r=json.load(open("/tmp/deployment.json")); print(r["endpoints"]); open("/tmp/kubeconfig", "w").write(r["artifacts"]["kubeconfig"])'
kubectl --kubeconfig /tmp/kubeconfig get nodes
```

The returned kubeconfig contains cluster administrator credentials. Keep the
deployment API and its response private. Docker-in-Docker requires a privileged
sandbox, so run untrusted submissions on a dedicated host or VM.

## Tests

```sh
uv run python -m unittest discover -s tests -v
RUN_DOCKER_SMOKE=1 uv run python -m unittest discover -s tests -p test_docker_smoke.py -v
RUN_KV_SMOKE=1 KV_SOLUTION_PATH=tasks/solutions/KeyValueStore/solution \
  uv run python -m unittest discover -s tests -p test_kv_integration.py -v
```
