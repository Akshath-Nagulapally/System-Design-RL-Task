#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
: "${DEPLOY_OUTPUT_DIR:?}"
: "${DEPLOY_TERRAFORM_OUTPUTS:?}"
: "${DEPLOY_SSH_PRIVATE_KEY:?}"
SEED_FILE="${KV_SEED_FILE:-/seed/kv.jsonl}"
[[ -r "$SEED_FILE" ]] || { echo 'seed file is not readable' >&2; exit 1; }
for tool in jq ssh scp go docker kubectl curl timeout; do
  command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }
done
SERVER_IP="$(jq -er '.server_ip.value' "$DEPLOY_TERRAFORM_OUTPUTS")"
mapfile -t NODE_IPS < <(jq -er '.droplet_ips.value[]' "$DEPLOY_TERRAFORM_OUTPUTS")
[[ "${#NODE_IPS[@]}" -ge 3 ]] || { echo 'reference deployment needs three Droplets' >&2; exit 1; }
SSH_OPTS=(-i "$DEPLOY_SSH_PRIVATE_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
  -o "UserKnownHostsFile=$DEPLOY_OUTPUT_DIR/known_hosts" -o ConnectTimeout=5)
PORT_FORWARD_PID=""
cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
    wait "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

for ip in "${NODE_IPS[@]}"; do
  ready=0
  for _ in $(seq 1 60); do
    if ssh "${SSH_OPTS[@]}" "root@$ip" 'cloud-init status --wait >/dev/null && test -x /usr/local/bin/k3s' </dev/null; then
      ready=1
      break
    fi
    sleep 3
  done
  [[ "$ready" -eq 1 ]] || { echo "K3s node $ip did not become ready" >&2; exit 1; }
done

ssh "${SSH_OPTS[@]}" "root@$SERVER_IP" 'cat /etc/rancher/k3s/k3s.yaml' > "$DEPLOY_OUTPUT_DIR/kubeconfig.local"
sed "s#server: https://127.0.0.1:6443#server: https://$SERVER_IP:6443#" \
  "$DEPLOY_OUTPUT_DIR/kubeconfig.local" > "$DEPLOY_OUTPUT_DIR/kubeconfig"
chmod 600 "$DEPLOY_OUTPUT_DIR/kubeconfig"
rm "$DEPLOY_OUTPUT_DIR/kubeconfig.local"
export KUBECONFIG="$DEPLOY_OUTPUT_DIR/kubeconfig"
kubectl wait --for=condition=Ready nodes --all --timeout=180s

mkdir -p bin
GOMAXPROCS=2 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -p 2 -trimpath -ldflags='-s -w' -o bin/kvstore ./cmd/kvstore
docker build --platform linux/amd64 --provenance=false -t kv-reference-api:1 .
docker save -o "$DEPLOY_OUTPUT_DIR/kv-api.tar" kv-reference-api:1
for ip in "${NODE_IPS[@]}"; do
  scp "${SSH_OPTS[@]}" "$DEPLOY_OUTPUT_DIR/kv-api.tar" "root@$ip:/tmp/kv-api.tar"
  ssh "${SSH_OPTS[@]}" "root@$ip" 'k3s ctr images import /tmp/kv-api.tar && rm /tmp/kv-api.tar'
done
rm "$DEPLOY_OUTPUT_DIR/kv-api.tar"

kubectl create namespace kvstore
kubectl apply -f manifests/etcd.yaml
kubectl -n kvstore rollout status statefulset/etcd --timeout=240s
kubectl apply -f manifests/api.yaml
kubectl -n kvstore rollout status deployment/kv-api --timeout=180s

kubectl -n kvstore port-forward --address 127.0.0.1 svc/etcd-client 2379:2379 \
  > "$DEPLOY_OUTPUT_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!
for _ in $(seq 1 30); do
  if grep -q 'Forwarding from 127.0.0.1:2379' "$DEPLOY_OUTPUT_DIR/port-forward.log"; then break; fi
  kill -0 "$PORT_FORWARD_PID" 2>/dev/null || { cat "$DEPLOY_OUTPUT_DIR/port-forward.log" >&2; exit 1; }
  sleep 1
done
ETCD_ENDPOINTS=127.0.0.1:2379 ./bin/kvstore import "$SEED_FILE"
kill "$PORT_FORWARD_PID" 2>/dev/null || true
wait "$PORT_FORWARD_PID" 2>/dev/null || true
PORT_FORWARD_PID=""

for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "http://$SERVER_IP/healthz" >/dev/null; then break; fi
  sleep 2
done
curl -fsS --max-time 5 "http://$SERVER_IP/healthz" >/dev/null
kubectl get --raw=/readyz >/dev/null
printf '{"endpoints":[{"name":"api","url":"http://%s"},{"name":"kubernetes","url":"https://%s:6443"}],"artifacts":[{"name":"kubeconfig","path":"kubeconfig"}]}\n' \
  "$SERVER_IP" "$SERVER_IP" > "$DEPLOY_OUTPUT_DIR/result.json"
