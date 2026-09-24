terraform {
  required_version = ">= 1.6.0"

  required_providers {
    digitalocean = {
      source = "digitalocean/digitalocean"
    }
  }
}

provider "digitalocean" {}

resource "digitalocean_ssh_key" "deployment" {
  name       = "kv-k3s-deployment"
  public_key = var.deployment_ssh_public_key
}

resource "digitalocean_droplet" "primary" {
  name       = "kv-k3s-0"
  region     = var.deployment_region
  size       = "s-2vcpu-2gb"
  image      = "ubuntu-24-04-x64"
  vpc_uuid   = var.deployment_vpc_id
  ssh_keys   = [digitalocean_ssh_key.deployment.id]
  monitoring = true
  user_data  = <<-EOF
    #cloud-config
    package_update: true
    packages:
      - docker.io
    runcmd:
      - |
        set -euo pipefail
        private_ip="$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/private/0/ipv4/address)"
        public_ip="$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address)"
        curl -fsSL https://get.k3s.io | INSTALL_K3S_VERSION="v1.35.5+k3s1" K3S_TOKEN="${var.deployment_k3s_token}" sh -s - server \
          --cluster-init \
          --node-ip="$private_ip" \
          --node-external-ip="$public_ip" \
          --tls-san="$public_ip" \
          --disable-cloud-controller \
          --disable=traefik \
          --disable=servicelb
  EOF
}

resource "digitalocean_droplet" "members" {
  count      = 2
  name       = "kv-k3s-${count.index + 1}"
  region     = var.deployment_region
  size       = "s-2vcpu-2gb"
  image      = "ubuntu-24-04-x64"
  vpc_uuid   = var.deployment_vpc_id
  ssh_keys   = [digitalocean_ssh_key.deployment.id]
  monitoring = true
  user_data  = <<-EOF
    #cloud-config
    runcmd:
      - |
        set -euo pipefail
        private_ip="$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/private/0/ipv4/address)"
        public_ip="$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address)"
        until curl -kfsS "https://${digitalocean_droplet.primary.ipv4_address_private}:6443/readyz" >/dev/null; do sleep 2; done
        curl -fsSL https://get.k3s.io | INSTALL_K3S_VERSION="v1.35.5+k3s1" K3S_URL="https://${digitalocean_droplet.primary.ipv4_address_private}:6443" K3S_TOKEN="${var.deployment_k3s_token}" sh -s - server \
          --node-ip="$private_ip" \
          --node-external-ip="$public_ip" \
          --tls-san="$public_ip" \
          --disable-cloud-controller \
          --disable=traefik \
          --disable=servicelb
  EOF
}

resource "digitalocean_project_resources" "deployment" {
  project = var.deployment_project_id
  resources = concat(
    [digitalocean_droplet.primary.urn],
    digitalocean_droplet.members[*].urn,
    digitalocean_loadbalancer.api.urn,
  )
}

output "server_ip" {
  value = digitalocean_droplet.primary.ipv4_address
}

output "droplet_ips" {
  value = concat(
    [digitalocean_droplet.primary.ipv4_address],
    digitalocean_droplet.members[*].ipv4_address,
  )
}

resource "digitalocean_loadbalancer" "api" {
  name     = "kv-k3s-api"
  region   = var.deployment_region
  vpc_uuid = var.deployment_vpc_id
  droplet_ids = concat(
    [digitalocean_droplet.primary.id],
    digitalocean_droplet.members[*].id,
  )

  forwarding_rule {
    entry_port      = 80
    entry_protocol  = "http"
    target_port     = 30080
    target_protocol = "http"
  }

  healthcheck {
    protocol                 = "http"
    port                     = 30080
    path                     = "/healthz"
    check_interval_seconds   = 5
    response_timeout_seconds = 3
    healthy_threshold        = 2
    unhealthy_threshold      = 3
  }
}

output "api_ip" {
  value = digitalocean_loadbalancer.api.ip
}
