"""Job-scoped DigitalOcean Terraform deployment backend."""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .resource_policy import ResourceLimits, ResourcePolicyError, validate_plan
from .server import DockerEngine, DeploymentError, DeploymentService, MAX_ARTIFACT_BYTES, unpack_repository


class DigitalOceanAPIError(DeploymentError):
    def __init__(self, status: int):
        super().__init__(f"DigitalOcean API returned HTTP {status}")
        self.status = status


class DigitalOceanAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = Request("https://api.digitalocean.com/v2" + path, data=data, method=method,
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                contents = response.read()
                return json.loads(contents) if contents else {}
        except HTTPError as exc:
            raise DigitalOceanAPIError(exc.code) from exc
        except URLError as exc:
            raise DeploymentError("DigitalOcean API unavailable") from exc

    def sizes(self) -> dict[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        page = 1
        while True:
            response = self.request("GET", f"/sizes?per_page=200&page={page}")
            for size in response.get("sizes", []):
                if (isinstance(size.get("slug"), str) and type(size.get("vcpus")) is int
                        and type(size.get("memory")) is int):
                    result[size["slug"]] = size["vcpus"], size["memory"]
            if not response.get("links", {}).get("pages", {}).get("next"):
                return result
            page += 1

    def create_project(self, name: str) -> str:
        response = self.request("POST", "/projects", {"name": name,
            "description": "Isolated system-design task deployment", "purpose": "Web Application",
            "environment": "Development"})
        project_id = response.get("project", {}).get("id")
        if not isinstance(project_id, str):
            raise DeploymentError("DigitalOcean did not return a project ID")
        return project_id

    def default_vpc(self, region: str) -> tuple[str, str]:
        response = self.request("GET", "/vpcs?per_page=200")
        for vpc in response.get("vpcs", []):
            if vpc.get("region") == region and vpc.get("default"):
                return vpc["id"], vpc["ip_range"]
        created = self.request("POST", "/vpcs", {
            "name": f"system-design-default-{region}",
            "description": "Shared default network for system-design deployments",
            "region": region,
        }).get("vpc", {})
        if not created.get("id") or not created.get("ip_range"):
            raise DeploymentError("DigitalOcean did not return a default VPC")
        return created["id"], created["ip_range"]

    def assign(self, project_id: str, urns: list[str]) -> None:
        if urns:
            self.request("POST", f"/projects/{project_id}/resources", {"resources": urns})

    def delete_project(self, project_id: str) -> None:
        self.request("DELETE", f"/projects/{project_id}")

    def project_droplet_urns(self, project_id: str) -> set[str]:
        urns: set[str] = set()
        page = 1
        while True:
            response = self.request("GET", f"/projects/{project_id}/resources?per_page=200&page={page}")
            urns.update(resource["urn"] for resource in response.get("resources", [])
                        if isinstance(resource.get("urn"), str) and
                        resource["urn"].startswith("do:droplet:"))
            if not response.get("links", {}).get("pages", {}).get("next"):
                return urns
            page += 1

    def delete_droplet(self, droplet_id: int) -> None:
        self.request("DELETE", f"/droplets/{droplet_id}")

    def droplet_exists(self, droplet_id: int) -> bool:
        try:
            self.request("GET", f"/droplets/{droplet_id}")
            return True
        except DigitalOceanAPIError as exc:
            if exc.status == 404:
                return False
            raise


def _resource_values(module: dict):
    for resource in module.get("resources", []):
        yield resource
    for child in module.get("child_modules", []):
        yield from _resource_values(child)


def _result(output: Path, allowed_ips: set[str]) -> dict:
    try:
        result = json.loads((output / "result.json").read_text())
    except (OSError, ValueError) as exc:
        raise DeploymentError("missing or invalid result.json") from exc
    endpoints = result.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise DeploymentError("no endpoints declared")
    urls: dict[str, str] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise DeploymentError("invalid endpoint")
        name, url = endpoint.get("name"), endpoint.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not isinstance(name, str) or not name.isidentifier() or name in urls
                or parsed is None or parsed.scheme not in ("http", "https")
                or parsed.hostname not in allowed_ips or parsed.username or parsed.password):
            raise DeploymentError("invalid endpoint URL")
        urls[name] = url
    artifacts: dict[str, str] = {}
    for artifact in result.get("artifacts", []):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str):
            raise DeploymentError("invalid artifact")
        path = artifact.get("path")
        if not isinstance(path, str):
            raise DeploymentError("invalid artifact path")
        file = (output / path).resolve()
        if (not file.is_relative_to(output.resolve()) or not file.is_file()
                or file.stat().st_size > MAX_ARTIFACT_BYTES):
            raise DeploymentError("invalid artifact file")
        artifacts[artifact["name"]] = file.read_text()
    return {"endpoints": urls, "artifacts": artifacts}


class DigitalOceanDeploymentService(DeploymentService):
    requires_budget = True

    def __init__(self, state_dir: Path, token: str, region: str, seed: Path):
        super().__init__(state_dir, engine=None)
        self.api = DigitalOceanAPI(token)
        self.region = region
        self.seed = seed
        self._mutation_lock = threading.Lock()

    @staticmethod
    def _terraform(directory: Path, environment: dict[str, str], *args: str,
                   timeout: int = 300) -> str:
        completed = subprocess.run(["terraform", *args], cwd=directory, env=environment,
                                   capture_output=True, text=True, timeout=timeout)
        if completed.returncode:
            raise DeploymentError(f"terraform {args[0]} failed: {completed.stderr[-1500:]}")
        return completed.stdout

    def run(self, archive: bytes, budget: dict | None = None) -> dict:
        limits = ResourceLimits.parse(budget)
        job_id = uuid.uuid4().hex[:12]
        (self.state_dir / "job_id").write_text(job_id)
        job_dir = self.state_dir / job_id
        repository, output = job_dir / "repository", job_dir / "output"
        repository.mkdir(parents=True, mode=0o700)
        output.mkdir(mode=0o700)
        project_id: str | None = None
        try:
            unpack_repository(archive, repository)
            terraform_dir = repository / "terraform"
            if not terraform_dir.is_dir() or not list(terraform_dir.glob("*.tf")):
                raise ResourcePolicyError("terraform root module missing")
            project_id = self.api.create_project(f"system-design-{job_id}")
            (job_dir / "project_id").write_text(project_id)
            key = job_dir / "deployment-key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
            key.chmod(0o600)
            environment = os.environ.copy()
            environment["DIGITALOCEAN_TOKEN"] = self.api.token
            environment.update({"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"})
            vpc_id, vpc_cidr = self.api.default_vpc(self.region)
            variables = job_dir / "terraform-inputs.json"
            variables.write_text(json.dumps({
                "deployment_project_id": project_id,
                "deployment_region": self.region,
                "deployment_ssh_public_key": key.with_suffix(".pub").read_text().strip(),
                "deployment_k3s_token": secrets.token_urlsafe(32),
                "deployment_vpc_id": vpc_id,
                "deployment_vpc_cidr": vpc_cidr,
            }))
            variables.chmod(0o600)
            self._terraform(terraform_dir, environment, "init", "-input=false", timeout=300)
            self._terraform(terraform_dir, environment, "plan", "-input=false",
                            f"-var-file={variables}", "-out=approved.tfplan",
                            timeout=300)
            plan = json.loads(self._terraform(terraform_dir, environment, "show", "-json",
                                              "approved.tfplan", timeout=60))
            totals = validate_plan(plan, self.api.sizes(), limits)
            (job_dir / "capacity.json").write_text(json.dumps({"budget": budget, "planned": totals}))
            self._terraform(terraform_dir, environment, "apply", "-input=false", "-auto-approve",
                            "approved.tfplan", timeout=900)
            state = json.loads(self._terraform(terraform_dir, environment, "show", "-json", timeout=60))
            urns = []
            droplets = []
            for resource in _resource_values(state.get("values", {}).get("root_module", {})):
                if resource.get("type") == "digitalocean_droplet":
                    values = resource.get("values", {})
                    urn = values.get("urn")
                    identifier = values.get("id")
                    if (not isinstance(urn, str) or not isinstance(identifier, (str, int))
                            or not str(identifier).isdigit() or
                            urn != f"do:droplet:{identifier}"):
                        raise DeploymentError("Droplet state has no valid identity")
                    urns.append(urn)
                    droplets.append({"id": int(identifier), "urn": urn, "name": values.get("name")})
            self.api.assign(project_id, urns)
            droplets_file = job_dir / "droplets.json"
            droplets_file.write_text(json.dumps(droplets))
            droplets_file.chmod(0o600)
            outputs = self._terraform(terraform_dir, environment, "output", "-json", timeout=60)
            output_path = output / "terraform-outputs.json"
            output_path.write_text(outputs)
            output_path.chmod(0o600)
            seed_path = job_dir / "seed" / "kv.jsonl"
            seed_path.parent.mkdir(mode=0o700)
            shutil.copyfile(self.seed, seed_path)
            seed_path.chmod(0o400)
            engine = DockerEngine("deployment-sandbox:latest", "deployment-proxy:latest",
                                  "127.0.0.1", [(seed_path, "/seed/kv.jsonl"),
                                                (key, "/credentials/ssh-key")],
                                  cpu_limit="4", memory_limit="4g")
            network, sandbox = engine.create(job_id, repository, output)
            try:
                engine.deploy(sandbox, job_dir / "deploy.log", {
                    "DEPLOY_TERRAFORM_OUTPUTS": "/deploy-output/terraform-outputs.json",
                    "DEPLOY_SSH_PRIVATE_KEY": "/credentials/ssh-key",
                    "KV_SEED_FILE": "/seed/kv.jsonl",
                })
            finally:
                engine.cleanup(network, [sandbox])
            allowed_ips = set(json.loads(outputs)["droplet_ips"]["value"])
            response = _result(output, allowed_ips)
            response_file = job_dir / "response.json"
            response_file.write_text(json.dumps(response))
            response_file.chmod(0o600)
            return response
        except Exception:
            (self.state_dir / "failed").write_text("error deployment unsuccessful")
            if project_id:
                try:
                    self.cleanup()
                except Exception:
                    pass
            raise

    def crash(self, count: int = 1) -> dict:
        if type(count) is not int or count <= 0:
            raise ValueError("crash count must be a positive integer")
        with self._mutation_lock:
            job_file = self.state_dir / "job_id"
            if not job_file.is_file():
                raise ValueError("no deployment to crash")
            job_dir = self.state_dir / job_file.read_text().strip()
            if not (job_dir / "response.json").is_file():
                raise ValueError("deployment is not ready")
            project_id = (job_dir / "project_id").read_text().strip()
            droplets = json.loads((job_dir / "droplets.json").read_text())
            project_urns = self.api.project_droplet_urns(project_id)
            history_file = job_dir / "crashes.json"
            history = json.loads(history_file.read_text()) if history_file.is_file() else []
            crashed = {entry["id"] for entry in history}
            available = [droplet for droplet in droplets if droplet["urn"] in project_urns
                         and droplet["id"] not in crashed]
            if count > len(available):
                raise ValueError(f"requested {count} crashes but only {len(available)} Droplets remain")
            seed = secrets.randbits(64)
            selected = random.Random(seed).sample(available, count)
            removed = []
            for droplet in selected:
                self.api.delete_droplet(droplet["id"])
                for _ in range(30):
                    if not self.api.droplet_exists(droplet["id"]):
                        break
                    time.sleep(2)
                else:
                    raise DeploymentError("Droplet deletion was not confirmed")
                event = {"id": droplet["id"], "name": droplet["name"], "seed": seed}
                history.append(event)
                history_file.write_text(json.dumps(history))
                history_file.chmod(0o600)
                removed.append(event)
            return {"removed": removed, "remaining": len(available) - len(removed)}

    def cleanup(self) -> None:
        with self._mutation_lock:
            self._cleanup_unlocked()

    def _cleanup_unlocked(self) -> None:
        job_file = self.state_dir / "job_id"
        if not job_file.is_file():
            return
        job_dir = self.state_dir / job_file.read_text().strip()
        project_file = job_dir / "project_id"
        terraform_dir = job_dir / "repository" / "terraform"
        if terraform_dir.is_dir() and (terraform_dir / "terraform.tfstate").is_file():
            environment = os.environ.copy()
            environment["DIGITALOCEAN_TOKEN"] = self.api.token
            variables = job_dir / "terraform-inputs.json"
            self._terraform(terraform_dir, environment, "destroy", "-input=false", "-auto-approve",
                            f"-var-file={variables}", timeout=900)
        if project_file.is_file():
            project_id = project_file.read_text().strip()
            for attempt in range(6):
                try:
                    self.api.delete_project(project_id)
                    break
                except DigitalOceanAPIError as exc:
                    if exc.status == 404:
                        break
                    if exc.status not in (409, 422) or attempt == 5:
                        raise
                    time.sleep(2)
            project_file.unlink()
