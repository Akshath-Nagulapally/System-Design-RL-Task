#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER=kv-store
IMAGE=kvstore:local

if [[ -z "${DEPLOY_OUTPUT_DIR:-}" ]]; then
  echo "DEPLOY_OUTPUT_DIR is not set" >&2
  exit 1
fi
mkdir -p "$DEPLOY_OUTPUT_DIR"
OUTPUT="$(cd "$DEPLOY_OUTPUT_DIR" && pwd)"

cleanup() {
  k3d kubeconfig get "$CLUSTER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Building service image..."
docker build --pull=false -t "$IMAGE" "$ROOT"

echo "Creating three-node K3s cluster..."
k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
k3d cluster create "$CLUSTER" \
  --api-port 6443 \
  --servers 1 \
  --agents 2 \
  --port '8080:30080@server:0' \
  --k3s-arg '--disable=traefik@server:0' \
  --wait

KUBECONFIG="$OUTPUT/kubeconfig"
k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"
export KUBECONFIG

echo "Importing images..."
k3d image import "$IMAGE" -c "$CLUSTER"

echo "Deploying etcd quorum and API..."
kubectl apply -f "$ROOT/k8s/etcd.yaml"
kubectl apply -f "$ROOT/k8s/api.yaml"

echo "Waiting for etcd quorum..."
kubectl wait --for=condition=ready pod -l app=etcd --timeout=300s

echo "Waiting for API..."
kubectl wait --for=condition=ready pod -l app=kv-api --timeout=300s

echo "Importing seed data..."
IMPORT_POD=kv-seed-import
kubectl delete pod "$IMPORT_POD" --ignore-not-found >/dev/null
kubectl run "$IMPORT_POD" \
  --image="kvstore:local" \
  --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"kv-seed-import","image":"kvstore:local","command":["sleep"],"args":["300"]}],"restartPolicy":"Never"}}' >/dev/null
kubectl wait --for=condition=ready pod "$IMPORT_POD" --timeout=120s
kubectl exec -i "$IMPORT_POD" -- \
  env ETCD_ENDPOINTS=http://etcd-0.etcd:2379,http://etcd-1.etcd:2379,http://etcd-2.etcd:2379 \
  /usr/local/bin/kvimport /dev/stdin < /seed/kv.jsonl
kubectl delete pod "$IMPORT_POD" --ignore-not-found >/dev/null

echo "Verifying endpoint..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8080/healthz >/dev/null
curl -fsS http://127.0.0.1:8080/v1/kv/welcome | grep -q '"hello"'

cat > "$OUTPUT/result.json" <<JSON
{
  "endpoints": [
    {"name": "api", "scheme": "http", "port": 8080},
    {"name": "kubernetes", "scheme": "https", "port": 6443}
  ],
  "artifacts": [
    {"name": "kubeconfig", "path": "kubeconfig"}
  ]
}
JSON

echo "Deployment complete"
