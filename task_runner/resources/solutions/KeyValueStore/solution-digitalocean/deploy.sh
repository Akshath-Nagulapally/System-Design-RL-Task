#!/usr/bin/env bash
set -euo pipefail

cd /app
: "${DEPLOY_TERRAFORM_OUTPUTS:?missing Terraform outputs}"
: "${DEPLOY_SSH_PRIVATE_KEY:?missing SSH key}"
: "${DEPLOY_OUTPUT_DIR:?missing output directory}"
: "${KV_SEED_FILE:?missing seed file}"

mkdir -p "$DEPLOY_OUTPUT_DIR"
rm -f "$DEPLOY_OUTPUT_DIR/result.json"
OUTPUTS="$DEPLOY_TERRAFORM_OUTPUTS"
KEY="$DEPLOY_SSH_PRIVATE_KEY"
OUT="$DEPLOY_OUTPUT_DIR"
mapfile -t PUBLIC_IPS < <(jq -r '.droplet_ips.value[]' "$OUTPUTS")
mapfile -t PRIVATE_IPS < <(jq -r '.private_ips.value[]' "$OUTPUTS")
LB_IP=$(jq -er '.loadbalancer_ip.value' "$OUTPUTS")
TOKEN=$(jq -er '.k3s_token.value' "$OUTPUTS")
[[ ${#PUBLIC_IPS[@]} -eq 3 && ${#PRIVATE_IPS[@]} -eq 3 ]] || { echo "expected three droplets" >&2; exit 1; }
TOKEN_B64=$(printf %s "$TOKEN" | base64 | tr -d '\n')
unset TOKEN
SSH_OPTIONS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
KUBECONFIG="$OUT/kubeconfig"
export KUBECONFIG

on_error() {
  echo "Deployment failed; diagnostic logs follow:" >&2
  if [[ -f "$KUBECONFIG" ]]; then
    kubectl -n kv get pods -o wide >&2 || true
    kubectl -n kv get events --sort-by=.lastTimestamp >&2 || true
  fi
}
trap on_error ERR

for ip in "${PUBLIC_IPS[@]}"; do
  echo "Waiting for SSH on $ip"
  ready=false
  for attempt in $(seq 1 90); do
    if ssh "${SSH_OPTIONS[@]}" "root@$ip" true 2>/dev/null; then
      ready=true
      break
    fi
    sleep 3
  done
  [[ $ready == true ]] || { echo "SSH unavailable on $ip" >&2; exit 1; }
done

printf 'Building API image concurrently with K3s installation\n'
docker build --platform linux/amd64 -t docker.io/library/kv-api:local . > "$OUT/build.log" 2>&1 &
BUILD_PID=$!

install_node() {
  local index="$1" mode="$2"
  echo "Installing K3s on ${PUBLIC_IPS[index]} ($mode)"
  ssh "${SSH_OPTIONS[@]}" "root@${PUBLIC_IPS[index]}" \
    "bash -s -- '${PRIVATE_IPS[index]}' '${PRIVATE_IPS[0]}' '${PUBLIC_IPS[index]}' '$LB_IP' '$TOKEN_B64' '$mode'" <<'REMOTE'
set -euo pipefail
private_ip=$1
server_ip=$2
public_ip=$3
lb_ip=$4
token=$(printf %s "$5" | base64 -d)
mode=$6
interface=$(ip -o -4 addr show | awk -v address="$private_ip" 'index($4, address "/") == 1 {print $2; exit}')
[[ -n "$interface" ]] || { echo "private interface missing for $private_ip" >&2; exit 1; }
options=(server --node-ip "$private_ip" --node-external-ip "$public_ip" --advertise-address "$private_ip" --flannel-iface "$interface" --tls-san "$lb_ip" --tls-san "$public_ip" --disable traefik --disable servicelb --disable metrics-server)
if [[ $mode == initial ]]; then
  options+=(--cluster-init)
else
  options+=(--server "https://$server_ip:6443")
fi
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='v1.35.5+k3s1' K3S_TOKEN="$token" sh -s - "${options[@]}"
REMOTE
}

install_node 0 initial
install_node 1 join > "$OUT/install-1.log" 2>&1 &
INSTALL_ONE=$!
install_node 2 join > "$OUT/install-2.log" 2>&1 &
INSTALL_TWO=$!
wait "$INSTALL_ONE" || { cat "$OUT/install-1.log" >&2; exit 1; }
wait "$INSTALL_TWO" || { cat "$OUT/install-2.log" >&2; exit 1; }

ssh "${SSH_OPTIONS[@]}" "root@${PUBLIC_IPS[0]}" cat /etc/rancher/k3s/k3s.yaml \
  | sed "s/127\.0\.0\.1/${PUBLIC_IPS[0]}/g" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"

for attempt in $(seq 1 90); do
  if [[ $(kubectl get nodes --no-headers 2>/dev/null | wc -l) -eq 3 ]]; then
    break
  fi
  sleep 2
done
kubectl wait --for=condition=Ready nodes --all --timeout=180s
[[ $(kubectl get nodes --no-headers | wc -l) -eq 3 ]] || { echo "three nodes did not join" >&2; exit 1; }
core_dns_patch=$(kubectl -n kube-system get deployment/coredns -o json | jq -c '{spec:{replicas:3,template:{spec:{affinity:{podAntiAffinity:{requiredDuringSchedulingIgnoredDuringExecution:[{labelSelector:.spec.selector,topologyKey:"kubernetes.io/hostname"}]}}}}}}')
kubectl -n kube-system patch deployment/coredns --type=merge -p "$core_dns_patch"
kubectl -n kube-system rollout status deployment/coredns --timeout=120s

if ! wait "$BUILD_PID"; then
  cat "$OUT/build.log" >&2
  exit 1
fi
echo "Distributing API image"
IMAGE_TAR="$OUT/image.tar"
docker save -o "$IMAGE_TAR" docker.io/library/kv-api:local
for ip in "${PUBLIC_IPS[@]}"; do
  ssh "${SSH_OPTIONS[@]}" "root@$ip" 'k3s ctr -n k8s.io images import -' < "$IMAGE_TAR" > /dev/null
done
rm -f "$IMAGE_TAR"

kubectl apply -f k8s/kv.yaml
kubectl -n kv rollout status statefulset/etcd --timeout=360s
kubectl -n kv rollout status daemonset/kv-api --timeout=180s

api_pod=$(kubectl -n kv get pod -l app=kv-api -o jsonpath='{.items[0].metadata.name}')
[[ -n $api_pod ]] || { echo "no API pod" >&2; exit 1; }
echo "Importing seed"
seed_key=$(awk 'NF {print; exit}' "$KV_SEED_FILE" | jq -er '.key')
kubectl -n kv exec -i "$api_pod" -- /kv import < "$KV_SEED_FILE"

ready=false
for attempt in $(seq 1 60); do
  if curl --fail --silent --max-time 3 "http://$LB_IP/healthz" > /dev/null && \
     curl --fail --silent --max-time 3 "http://$LB_IP/v1/kv/$seed_key" > /dev/null; then
    ready=true
    break
  fi
  sleep 2
done
[[ $ready == true ]] || { echo "external API did not become ready" >&2; exit 1; }

sed -i "s/${PUBLIC_IPS[0]}/$LB_IP/g" "$KUBECONFIG"

jq -n --arg api "http://$LB_IP" --arg kube "https://$LB_IP:6443" '{
  endpoints: [
    {name: "api", scheme: "http", url: $api},
    {name: "kubernetes", scheme: "https", url: $kube}
  ],
  artifacts: [{name: "kubeconfig", path: "kubeconfig"}]
}' > "$OUT/result.json"
echo "Deployment ready at http://$LB_IP"
