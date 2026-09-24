variable "deployment_project_id" {
  type = string
}

variable "deployment_region" {
  type = string
}

variable "deployment_vpc_id" {
  type = string
}

variable "deployment_vpc_cidr" {
  type = string
}

variable "deployment_ssh_public_key" {
  type = string
}

variable "deployment_k3s_token" {
  type      = string
  sensitive = true
}
