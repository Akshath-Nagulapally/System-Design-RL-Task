"""Capacity policy for DigitalOcean Terraform plans."""

from __future__ import annotations

from dataclasses import dataclass


class ResourcePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ResourceLimits:
    cpu_cores: int
    memory_mb: int

    @classmethod
    def parse(cls, value: object) -> "ResourceLimits":
        if not isinstance(value, dict) or set(value) != {"cpu_cores", "memory_mb"}:
            raise ResourcePolicyError("resource limits require cpu_cores and memory_mb")
        cpu, memory = value["cpu_cores"], value["memory_mb"]
        if type(cpu) is not int or type(memory) is not int or cpu <= 0 or memory <= 0:
            raise ResourcePolicyError("resource limits must be positive integers")
        return cls(cpu, memory)


def _modules(module: dict):
    yield module
    for child in module.get("child_modules", []):
        yield from _modules(child)


NONCOMPUTE = {
    "digitalocean_ssh_key", "digitalocean_firewall",
    "digitalocean_project_resources", "digitalocean_tag", "digitalocean_volume",
    "digitalocean_volume_attachment", "digitalocean_reserved_ip",
    "digitalocean_reserved_ip_assignment", "digitalocean_loadbalancer",
}
PROVIDER = "registry.terraform.io/digitalocean/digitalocean"


def validate_plan(plan: dict, sizes: dict[str, tuple[int, int]], limits: ResourceLimits) -> dict[str, int]:
    """Measure final planned Droplets; reject hidden or unmeasurable compute."""
    if plan.get("errored") or not isinstance(plan.get("planned_values"), dict):
        raise ResourcePolicyError("Terraform plan is incomplete")
    config = plan.get("configuration", {})
    for module in _modules(config.get("root_module", {})):
        for resource in module.get("resources", []):
            if resource.get("provisioners"):
                raise ResourcePolicyError("Terraform provisioners are not allowed")
            provider = resource.get("provider_config_key", "")
            if provider and provider not in config.get("provider_config", {}):
                raise ResourcePolicyError("unknown Terraform provider")
    for provider in config.get("provider_config", {}).values():
        if provider.get("full_name") != PROVIDER:
            raise ResourcePolicyError("only the DigitalOcean Terraform provider is allowed")
    root = plan["planned_values"].get("root_module")
    if not isinstance(root, dict):
        raise ResourcePolicyError("Terraform plan has no root module")
    totals = {"cpu_cores": 0, "memory_mb": 0}
    droplets = 0
    for module in _modules(root):
        for resource in module.get("resources", []):
            if resource.get("mode") != "managed":
                continue
            address = resource.get("address", "resource")
            if resource.get("provider_name") != PROVIDER:
                raise ResourcePolicyError(f"unsupported provider for {address}")
            kind = resource.get("type")
            if kind in NONCOMPUTE:
                continue
            if kind != "digitalocean_droplet":
                raise ResourcePolicyError(f"unmeasurable resource type: {kind}")
            slug = resource.get("values", {}).get("size")
            if not isinstance(slug, str) or slug not in sizes:
                raise ResourcePolicyError(f"unknown Droplet size for {address}")
            cpu, memory = sizes[slug]
            totals["cpu_cores"] += cpu
            totals["memory_mb"] += memory
            droplets += 1
    if not droplets:
        raise ResourcePolicyError("Terraform plan has no Droplets")
    if totals["cpu_cores"] > limits.cpu_cores or totals["memory_mb"] > limits.memory_mb:
        raise ResourcePolicyError(
            f"planned capacity exceeds budget: {totals['cpu_cores']}/{limits.cpu_cores} CPU, "
            f"{totals['memory_mb']}/{limits.memory_mb} MB memory"
        )
    return totals
