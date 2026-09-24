#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEPLOY_TERRAFORM_OUTPUTS:?DEPLOY_TERRAFORM_OUTPUTS is required}"
: "${DEPLOY_SSH_PRIVATE_KEY:?DEPLOY_SSH_PRIVATE_KEY is required}"
: "${DEPLOY_OUTPUT_DIR:?DEPLOY_OUTPUT_DIR is required}"
: "${KV_SEED_FILE:?KV_SEED_FILE is required}"

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OUTPUT_DIR="$(mkdir -p "${DEPLOY_OUTPUT_DIR}" && cd "${DEPLOY_OUTPUT_DIR}" && pwd)"
readonly SERVER_IP="$(jq -r '.api_ip.value' "$DEPLOY_TERRAFORM_OUTPUTS")"
readonly CONTROL_PLANE_IP="$(jq -r '.droplet_ips.value[0]' "$DEPLOY_TERRAFORM_OUTPUTS")"
mapfile -t DROPLET_IPS < <(jq -r '.droplet_ips.value[]' "$DEPLOY_TERRAFORM_OUTPUTS")

if [[ -z "$SERVER_IP" || -z "$CONTROL_PLANE_IP" || "${#DROPLET_IPS[@]}" -ne 3 ]]; then
  echo "Terraform outputs are missing expected IP addresses" >&2
  exit 1
fi

readonly SSH_TMP_DIR="$(mktemp -d)"
readonly IMAGE_TMP="$SSH_TMP_DIR/kv-api-image.tar.gz"
trap 'rm -rf "$SSH_TMP_DIR"' EXIT
install -m 600 "$DEPLOY_SSH_PRIVATE_KEY" "$SSH_TMP_DIR/id_ed25519"

ssh_run() {
  local host="$1"
  shift
  ssh -i "$SSH_TMP_DIR/id_ed25519" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=6 "root@${host}" "$@"
}

scp_to() {
  local host="$1" source="$2" destination="$3"
  scp -i "$SSH_TMP_DIR/id_ed25519" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$source" "root@${host}:${destination}"
}

echo "Waiting for cloud-init and the K3s control plane..."
for attempt in $(seq 1 90); do
  if ssh_run "$CONTROL_PLANE_IP" 'cloud-init status --wait && k3s --version' > "$SSH_TMP_DIR/k3s-version"; then
    grep -q 'v1.35.5+k3s1' "$SSH_TMP_DIR/k3s-version"
    break
  fi
  if (( attempt == 90 )); then
    echo "Timed out waiting for K3s on $CONTROL_PLANE_IP" >&2
    exit 1
  fi
  sleep 5
done

echo "Waiting for all K3s nodes..."
for attempt in $(seq 1 60); do
  if ssh_run "$CONTROL_PLANE_IP" \
    'test "$(kubectl get nodes --no-headers | awk '\''$2 == "Ready" { count++ } END { print count+0 }'\'')" -eq 3'; then
    break
  fi
  if (( attempt == 60 )); then
    echo "Timed out waiting for three ready K3s nodes" >&2
    exit 1
  fi
  sleep 10
done

echo "Building API image remotely..."
ssh_run "$CONTROL_PLANE_IP" 'rm -rf /tmp/kv-api-src && mkdir -p /tmp/kv-api-src'
tar --exclude='./kv' --exclude='.git' --exclude='terraform/.terraform' --exclude='terraform/.terraform' --exclude='terraform/.terraform.lock.hcl' \
  -czf - -C "$ROOT_DIR" . | \
  ssh_run "$CONTROL_PLANE_IP" 'tar -xzf - -C /tmp/kv-api-src'
ssh_run "$CONTROL_PLANE_IP" 'docker build --platform=linux/amd64 --pull -t kv-api:local /tmp/kv-api-src'
ssh_run "$CONTROL_PLANE_IP" 'docker save kv-api:local | gzip -1 > /tmp/kv-api-image.tar.gz'
scp_to "$CONTROL_PLANE_IP" '/tmp/kv-api-image.tar.gz' "$IMAGE_TMP"

echo "Distributing API image..."
for ip in "${DROPLET_IPS[@]}"; do
  scp_to "$ip" "$IMAGE_TMP" '/tmp/kv-api-image.tar.gz'
  ssh_run "$ip" 'gunzip -c /tmp/kv-api-image.tar.gz > /tmp/kv-api-image.tar && k3s ctr -n k8s.io images import /tmp/kv-api-image.tar'
done

echo "Creating kubeconfig artifact and applying manifests..."
ssh_run "$CONTROL_PLANE_IP" 'cat /etc/rancher/k3s/k3s.yaml' | \
  sed "s/https:\/\/127.0.0.1:6443/https:\/\/${CONTROL_PLANE_IP}:6443/" > "$OUTPUT_DIR/kubeconfig"
chmod 600 "$OUTPUT_DIR/kubeconfig"
export KUBECONFIG="$OUTPUT_DIR/kubeconfig"

kubectl apply -f "$ROOT_DIR/manifests"
kubectl --namespace kv wait --for=condition=ready pod \
  --selector=app.kubernetes.io/name=etcd --timeout=360s
kubectl --namespace kv rollout status deployment/kv-api --timeout=360s

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error "http://${SERVER_IP}/healthz" >/dev/null; then
    break
  fi
  if (( attempt == 30 )); then
    echo "Timed out waiting for the public API health endpoint" >&2
    exit 1
  fi
  sleep 2
done

echo "Importing seed data..."
kubectl --namespace kv exec -i deployment/kv-api -- /app -import < "$KV_SEED_FILE"

jq -n \
  --arg api "http://${SERVER_IP}" \
  --arg kubernetes "https://${CONTROL_PLANE_IP}:6443" \
  '{
    endpoints: [
      {name: "api", scheme: "http", url: $api},
      {name: "kubernetes", scheme: "https", url: $kubernetes}
    ],
    artifacts: [{name: "kubeconfig", path: "kubeconfig"}]
  }' > "$OUTPUT_DIR/result.json"

echo "Deployment complete. API: http://${SERVER_IP}"
