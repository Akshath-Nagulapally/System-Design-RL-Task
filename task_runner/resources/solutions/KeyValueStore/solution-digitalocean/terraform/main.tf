terraform {
  required_version = ">= 1.5.0"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.102"
    }
  }
}

provider "digitalocean" {}

variable "deployment_project_id" { type = string }
variable "deployment_region" { type = string }
variable "deployment_vpc_id" { type = string }
variable "deployment_vpc_cidr" { type = string }
variable "deployment_ssh_public_key" { type = string }
variable "deployment_k3s_token" {
  type      = string
  sensitive = true
}

resource "digitalocean_ssh_key" "deployment" {
  name       = "kv-${substr(var.deployment_project_id, 0, 8)}"
  public_key = trimspace(var.deployment_ssh_public_key)
}

resource "digitalocean_droplet" "node" {
  count    = 3
  name     = "kv-${substr(var.deployment_project_id, 0, 8)}-${count.index}"
  image    = "ubuntu-24-04-x64"
  region   = var.deployment_region
  size     = "s-2vcpu-4gb"
  vpc_uuid = var.deployment_vpc_id
  ssh_keys = [digitalocean_ssh_key.deployment.id]
}

resource "digitalocean_loadbalancer" "api" {
  name                     = "kv-${substr(var.deployment_project_id, 0, 8)}"
  region                   = var.deployment_region
  vpc_uuid                 = var.deployment_vpc_id
  project_id               = var.deployment_project_id
  droplet_ids              = digitalocean_droplet.node[*].id
  enable_backend_keepalive = true

  forwarding_rule {
    entry_protocol  = "http"
    entry_port      = 80
    target_protocol = "http"
    target_port     = 30080
  }

  forwarding_rule {
    entry_protocol  = "tcp"
    entry_port      = 6443
    target_protocol = "tcp"
    target_port     = 6443
  }

  healthcheck {
    protocol                 = "http"
    port                     = 30080
    path                     = "/healthz"
    check_interval_seconds   = 5
    response_timeout_seconds = 3
    healthy_threshold        = 2
    unhealthy_threshold      = 2
  }
}

output "server_ip" {
  value = digitalocean_droplet.node[0].ipv4_address
}

output "droplet_ips" {
  value = digitalocean_droplet.node[*].ipv4_address
}

output "private_ips" {
  value = digitalocean_droplet.node[*].ipv4_address_private
}

output "loadbalancer_ip" {
  value = digitalocean_loadbalancer.api.ip
}

output "k3s_token" {
  value     = var.deployment_k3s_token
  sensitive = true
}
