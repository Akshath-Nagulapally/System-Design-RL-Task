"""Compose the sole Harbor instruction from trusted task metadata."""

from __future__ import annotations

from pathlib import Path


CONTRACTS = Path(__file__).resolve().parent / "contracts"


def compose_instruction(prompt: Path, contract: str, cpu_cores: int, memory_mb: int) -> str:
    if contract != "digitalocean-k3s-v1":
        raise ValueError(f"unsupported deployment contract: {contract}")
    base = prompt.read_text().rstrip()
    deployment = (CONTRACTS / f"{contract}.md").read_text().rstrip()
    budget = ("# Resource budget\n\n"
              f"The entire deployment may provision at most {cpu_cores} virtual CPU cores "
              f"and {memory_mb} MB of Droplet memory. The server sums every planned "
              "Droplet's DigitalOcean size. An exact match is allowed; exceeding either "
              "limit fails deployment and earns zero credit.\n")
    return "\n\n".join((base, deployment, budget))
