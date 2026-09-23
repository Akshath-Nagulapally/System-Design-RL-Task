#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME=kv-reference
NAMESPACE=kvstore
API_IMAGE=kv-reference-api:1
SEED_FILE=${KV_SEED_FILE:-/seed/kv.jsonl}
DEADLINE=$(( $(date +%s) + 900 ))
PORT_FORWARD_PID=""

host_ip() {
  local address=""
  if [[ -n "${DEPLOY_HOST_IP:-}" ]]; then
    printf '%s' "$DEPLOY_HOST_IP"
    return
  fi
  if command -v ip >/dev/null 2>&1; then
    address="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -nE 's/.* src ([0-9.]+).*/\1/p' | head -n 1 || true)"
  fi
  if [[ -z "$address" ]] && command -v hostname >/dev/null 2>&1; then
    address="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "$address" ]] && command -v ipconfig >/dev/null 2>&1; then
    address="$(ipconfig getifaddr en0 2>/dev/null || true)"
  fi
  printf '%s' "$address"
}

remaining() {
  local seconds=$(( DEADLINE - $(date +%s) ))
  if (( seconds <= 0 )); then
    echo "deployment exceeded the 15-minute deadline" >&2
    exit 1
  fi
  printf '%s' "$seconds"
}

run() {
  timeout "$(remaining)s" "$@"
}

cleanup() {
  local result=$?
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
    wait "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
  if (( result != 0 )) && [[ -n "${KUBECONFIG:-}" && -f "$KUBECONFIG" ]]; then
    kubectl -n "$NAMESPACE" get pods -o wide >&2 || true
    kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp >&2 || true
  fi
  exit "$result"
}
trap cleanup EXIT

for tool in go docker k3d kubectl curl timeout; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done
[[ -r "$SEED_FILE" ]] || { echo "missing readable seed: $SEED_FILE" >&2; exit 1; }
[[ -n "${DEPLOY_OUTPUT_DIR:-}" && -d "$DEPLOY_OUTPUT_DIR" && -w "$DEPLOY_OUTPUT_DIR" ]] || {
  echo "DEPLOY_OUTPUT_DIR must name a writable directory" >&2
  exit 1
}
DEPLOY_HOST_IP="$(host_ip)"
[[ "$DEPLOY_HOST_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "could not determine the sandbox IPv4 address; set DEPLOY_HOST_IP" >&2
  exit 1
}
run docker info >/dev/null
if k3d cluster list "$CLUSTER_NAME" --no-headers 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  echo "cluster $CLUSTER_NAME already exists; deployment requires a fresh sandbox" >&2
  exit 1
fi

cd "$ROOT_DIR"
mkdir -p bin
echo "Building API before Kubernetes starts"
GOMAXPROCS=2 CGO_ENABLED=0 GOOS=linux GOARCH="$(go env GOARCH)" \
  run go build -p 2 -trimpath -ldflags='-s -w' -o bin/kvstore ./cmd/kvstore
IMPORT_BIN=./bin/kvstore
if [[ "$(go env GOOS)" != linux ]]; then
  GOMAXPROCS=2 CGO_ENABLED=0 run go build -p 2 -trimpath -ldflags='-s -w' -o bin/kvstore-host ./cmd/kvstore
  IMPORT_BIN=./bin/kvstore-host
fi
run docker build --provenance=false -t "$API_IMAGE" .

echo "Creating a three-node K3s cluster"
run k3d cluster create "$CLUSTER_NAME" \
  --servers 1 --agents 2 \
  --servers-memory 2560m --agents-memory 2048m \
  --api-port '0.0.0.0:6443' \
  --k3s-arg "--tls-san=${DEPLOY_HOST_IP}@server:*" \
  --port '0.0.0.0:8080:80@loadbalancer' \
  --kubeconfig-update-default=false \
  --kubeconfig-switch-context=false \
  --wait --timeout 180s

# The node caps leave room inside the sandbox for dockerd, image import and
# the seed importer. The outer sandbox must also enforce 6 vCPU / 8 GiB in
# aggregate, including these nested containers.
run docker update --cpus 2 k3d-${CLUSTER_NAME}-server-0 >/dev/null
run docker update --cpus 1.5 k3d-${CLUSTER_NAME}-agent-0 >/dev/null
run docker update --cpus 1.5 k3d-${CLUSTER_NAME}-agent-1 >/dev/null
run docker update --cpus 0.5 --memory 256m --memory-swap 256m k3d-${CLUSTER_NAME}-serverlb >/dev/null

export KUBECONFIG="$DEPLOY_OUTPUT_DIR/kubeconfig"
run k3d kubeconfig get "$CLUSTER_NAME" \
  | sed -E "s#server: https://[^ ]+#server: https://${DEPLOY_HOST_IP}:6443#" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"
run kubectl wait --for=condition=Ready node --all --timeout=120s
run kubectl label node k3d-${CLUSTER_NAME}-agent-0 benchmark-role=worker --overwrite
run kubectl label node k3d-${CLUSTER_NAME}-agent-1 benchmark-role=worker --overwrite

echo "Loading images and deploying storage"
run k3d image import "$API_IMAGE" -c "$CLUSTER_NAME"
run kubectl create namespace "$NAMESPACE"
run kubectl apply -f manifests/etcd.yaml
run kubectl -n "$NAMESPACE" rollout status statefulset/etcd --timeout="$(remaining)s"
run kubectl apply -f manifests/api.yaml
run kubectl -n "$NAMESPACE" rollout status deployment/kv-api --timeout="$(remaining)s"

echo "Importing seed data"
: > /tmp/${CLUSTER_NAME}-port-forward.log
kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 svc/etcd-client :2379 \
  > /tmp/${CLUSTER_NAME}-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!
FORWARD_PORT=""
for _ in $(seq 1 60); do
  FORWARD_PORT="$(sed -nE 's/^Forwarding from 127\.0\.0\.1:([0-9]+) -> 2379$/\1/p' /tmp/${CLUSTER_NAME}-port-forward.log | head -n 1)"
  if [[ -n "$FORWARD_PORT" ]]; then
    break
  fi
  kill -0 "$PORT_FORWARD_PID" 2>/dev/null || { cat /tmp/${CLUSTER_NAME}-port-forward.log >&2; exit 1; }
  sleep 1
done
if [[ -z "$FORWARD_PORT" ]]; then
  cat /tmp/${CLUSTER_NAME}-port-forward.log >&2
  exit 1
fi
ETCD_ENDPOINTS="127.0.0.1:$FORWARD_PORT" run "$IMPORT_BIN" import "$SEED_FILE"
kill "$PORT_FORWARD_PID" 2>/dev/null || true
wait "$PORT_FORWARD_PID" 2>/dev/null || true
PORT_FORWARD_PID=""

echo "Checking the public endpoint"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://${DEPLOY_HOST_IP}:8080/healthz" >/dev/null; then
    break
  fi
  remaining >/dev/null
  sleep 1
done
run curl -fsS --max-time 5 "http://${DEPLOY_HOST_IP}:8080/healthz" >/dev/null
run kubectl get --raw=/readyz >/dev/null

RESULT_TMP="$(mktemp "$DEPLOY_OUTPUT_DIR/result.json.XXXXXX")"
printf '%s\n' '{"endpoints":[{"name":"api","scheme":"http","port":8080},{"name":"kubernetes","scheme":"https","port":6443}],"artifacts":[{"name":"kubeconfig","path":"kubeconfig"}]}' > "$RESULT_TMP"
mv "$RESULT_TMP" "$DEPLOY_OUTPUT_DIR/result.json"
echo "Ready: http://${DEPLOY_HOST_IP}:8080"
