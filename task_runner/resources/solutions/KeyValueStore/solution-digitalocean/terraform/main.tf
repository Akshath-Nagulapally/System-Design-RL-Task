locals {
  prefix = "kv-${substr(var.deployment_project_id, 0, 8)}"
}

resource "digitalocean_ssh_key" "runner" {
  name       = "${local.prefix}-runner"
  public_key = var.deployment_ssh_public_key
}

resource "digitalocean_droplet" "server" {
  name      = "${local.prefix}-server"
  image     = "ubuntu-24-04-x64"
  region    = var.deployment_region
  size      = "s-2vcpu-2gb"
  vpc_uuid  = var.deployment_vpc_id
  ssh_keys  = [digitalocean_ssh_key.runner.id]
  user_data = <<-CLOUDINIT
    #!/usr/bin/env bash
    set -euo pipefail
    PRIVATE_IP=$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/private/0/ipv4/address)
    PUBLIC_IP=$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address)
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='v1.35.5+k3s1' K3S_TOKEN='${var.deployment_k3s_token}' \
      sh -s - server --node-ip "$PRIVATE_IP" --advertise-address "$PRIVATE_IP" \
      --flannel-iface eth1 \
      --tls-san "$PUBLIC_IP" --node-name '${local.prefix}-server'
  CLOUDINIT
}

resource "digitalocean_droplet" "worker" {
  count     = 2
  name      = "${local.prefix}-worker-${count.index}"
  image     = "ubuntu-24-04-x64"
  region    = var.deployment_region
  size      = "s-2vcpu-2gb"
  vpc_uuid  = var.deployment_vpc_id
  ssh_keys  = [digitalocean_ssh_key.runner.id]
  user_data = <<-CLOUDINIT
    #!/usr/bin/env bash
    set -euo pipefail
    PRIVATE_IP=$(curl -fsS http://169.254.169.254/metadata/v1/interfaces/private/0/ipv4/address)
    SERVER='${digitalocean_droplet.server.ipv4_address_private}'
    until curl -ksS --max-time 2 "https://$SERVER:6443/readyz" >/dev/null; do sleep 3; done
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='v1.35.5+k3s1' \
      K3S_URL="https://$SERVER:6443" K3S_TOKEN='${var.deployment_k3s_token}' \
      sh -s - agent --node-ip "$PRIVATE_IP" --flannel-iface eth1 \
      --node-name '${local.prefix}-worker-${count.index}' \
      --node-label benchmark-role=worker
  CLOUDINIT
}

resource "digitalocean_firewall" "kv" {
  name        = "${local.prefix}-firewall"
  droplet_ids = concat([digitalocean_droplet.server.id], digitalocean_droplet.worker[*].id)

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "6443"
    source_addresses = ["0.0.0.0/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "1-65535"
    source_addresses = [var.deployment_vpc_cidr]
  }
  inbound_rule {
    protocol         = "udp"
    port_range       = "1-65535"
    source_addresses = [var.deployment_vpc_cidr]
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0"]
  }
}

resource "digitalocean_project_resources" "kv" {
  project = var.deployment_project_id
  resources = concat(
    [digitalocean_droplet.server.urn],
    digitalocean_droplet.worker[*].urn,
  )
}

output "server_ip" {
  value = digitalocean_droplet.server.ipv4_address
}

output "droplet_ips" {
  value = concat([digitalocean_droplet.server.ipv4_address], digitalocean_droplet.worker[*].ipv4_address)
}
