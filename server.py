"""One-shot HTTP deployment runner for repository tarballs."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


LOG = logging.getLogger("deployment_server")
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
DEPLOY_TIMEOUT_SECONDS = 15 * 60 + 30


class DeploymentError(Exception):
    pass


def unpack_repository(data: bytes, destination: Path) -> None:
    """Extract a repository tarball without links or paths outside destination."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        with archive:
            members = archive.getmembers()
            if len(members) > 10_000 or sum(m.size for m in members) > MAX_ARCHIVE_BYTES:
                raise DeploymentError("archive too large")
            files = []
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                    raise DeploymentError("unsafe archive entry")
                parts = tuple(part for part in path.parts if part != ".")
                if parts:
                    files.append((member, parts))
            roots = {parts[0] for _, parts in files}
            strip_root = len(roots) == 1 and not any(len(parts) == 1 and member.isfile() for member, parts in files)
            for member, parts in files:
                relative = Path(*(parts[1:] if strip_root else parts))
                if not relative.parts:
                    continue
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                    target.chmod(member.mode & 0o777)
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise DeploymentError("invalid repository archive") from exc
    if not (destination / "deploy.sh").is_file():
        raise DeploymentError("deploy.sh missing from repository root")


def load_result(output: Path) -> tuple[list[dict], dict[str, Path]]:
    try:
        result_file = (output / "result.json").resolve()
        if not result_file.is_relative_to(output.resolve()) or result_file.stat().st_size > MAX_ARTIFACT_BYTES:
            raise DeploymentError("invalid result.json")
        result = json.loads(result_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("missing or invalid result.json") from exc
    endpoints = result.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise DeploymentError("no endpoints declared")
    names: set[str] = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise DeploymentError("invalid endpoint")
        name, scheme, port = (endpoint.get(key) for key in ("name", "scheme", "port"))
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name)
                or name in names or scheme not in ("http", "https", "tcp")
                or type(port) is not int or not 1 <= port <= 65535):
            raise DeploymentError("invalid endpoint")
        names.add(name)
    artifacts: dict[str, Path] = {}
    for artifact in result.get("artifacts", []):
        if not isinstance(artifact, dict):
            raise DeploymentError("invalid artifact")
        name, path = artifact.get("name"), artifact.get("path")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name)
                or name in artifacts or not isinstance(path, str)):
            raise DeploymentError("invalid artifact")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise DeploymentError("unsafe artifact path")
        resolved = (output / path).resolve()
        if not resolved.is_relative_to(output.resolve()) or not resolved.is_file() or resolved.stat().st_size > MAX_ARTIFACT_BYTES:
            raise DeploymentError("invalid artifact file")
        artifacts[name] = resolved
    return endpoints, artifacts


def rewrite_kubeconfig(contents: str, public_url: str) -> str:
    """Keep the original API certificate name while changing the dial address."""
    matches = list(re.finditer(r"(?m)^([ \t]*)server:[ \t]*(https://\S+)[ \t]*$", contents))
    if len(matches) != 1:
        raise DeploymentError("kubeconfig must contain one API server")
    match = matches[0]
    tls_name = urlsplit(match.group(2)).hostname
    if not tls_name:
        raise DeploymentError("invalid kubeconfig API server")
    indent = match.group(1)
    replacement = f"{indent}server: {public_url}\n{indent}tls-server-name: {tls_name}"
    return contents[:match.start()] + replacement + contents[match.end():]


class DockerEngine:
    def __init__(self, image: str, proxy_image: str, public_host: str,
                 input_mounts: list[tuple[Path, str]] | None = None):
        self.image = image
        self.proxy_image = proxy_image
        self.public_host = public_host
        self.input_mounts = input_mounts or []

    @staticmethod
    def command(*args: str, timeout: int = 60) -> str:
        completed = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout)
        if completed.returncode:
            raise DeploymentError(completed.stderr.strip() or "docker command failed")
        return completed.stdout.strip()

    def create(self, job_id: str, repository: Path, output: Path) -> tuple[str, str]:
        network = f"deploy-{job_id}"
        sandbox = f"sandbox-{job_id}"
        self.command("network", "create", network)
        try:
            args = ["run", "-d", "--name", sandbox, "--network", network, "--privileged",
                    "--cpus", "6", "--memory", "8g", "--memory-swap", "8g",
                    "--mount", f"type=bind,src={repository},dst=/app",
                    "--mount", f"type=bind,src={output},dst=/deploy-output"]
            for source, target in self.input_mounts:
                args += ["--mount", f"type=bind,src={source},dst={target},readonly"]
            self.command(*args, self.image)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    self.command("exec", sandbox, "docker", "info", timeout=10)
                    self.command("exec", sandbox, "openrc", "default", timeout=30)
                    return network, sandbox
                except (DeploymentError, subprocess.TimeoutExpired):
                    time.sleep(1)
            raise DeploymentError("Docker daemon did not start in sandbox")
        except Exception:
            self.cleanup(network, [sandbox])
            raise

    def deploy(self, sandbox: str, log_path: Path) -> None:
        with log_path.open("w") as log:
            log_path.chmod(0o600)
            completed = subprocess.run(
                ["docker", "exec", "-e", "DEPLOY_OUTPUT_DIR=/deploy-output", "-w", "/app",
                 sandbox, "bash", "./deploy.sh"],
                stdout=log, stderr=subprocess.STDOUT, timeout=DEPLOY_TIMEOUT_SECONDS,
            )
        if completed.returncode:
            raise DeploymentError(f"deploy.sh failed; see {log_path}")

    def expose(self, network: str, sandbox: str, name: str, scheme: str, port: int) -> tuple[str, str]:
        proxy = f"proxy-{sandbox}-{name}"[:63]
        try:
            self.command("exec", sandbox, "bash", "-c",
                         f"echo > /dev/tcp/127.0.0.1/{port}", timeout=5)
            bind_address = "127.0.0.1" if self.public_host in ("127.0.0.1", "localhost") else "0.0.0.0"
            self.command("run", "-d", "--name", proxy, "--network", network,
                         "--cpus", "0.25", "--memory", "64m", "-p", f"{bind_address}::9000",
                         self.proxy_image, "TCP-LISTEN:9000,fork,reuseaddr",
                         f"TCP:{sandbox}:{port}")
            mapping = self.command("port", proxy, "9000/tcp").splitlines()[0]
            public_port = int(mapping.rsplit(":", 1)[1])
            url = f"{scheme}://{self.public_host}:{public_port}"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    self.command("exec", proxy, "nc", "-z", "-w", "2", sandbox, str(port), timeout=5)
                    with socket.create_connection(("127.0.0.1", public_port), timeout=1):
                        return proxy, url
                except (OSError, DeploymentError, subprocess.TimeoutExpired):
                    time.sleep(0.25)
            raise DeploymentError(f"endpoint {name} did not become reachable")
        except Exception:
            self.cleanup(None, [proxy])
            raise

    def cleanup(self, network: str | None, containers: list[str]) -> None:
        for container in reversed(containers):
            try:
                self.command("rm", "-f", container)
            except DeploymentError:
                LOG.exception("could not remove container %s", container)
        if network:
            try:
                self.command("network", "rm", network)
            except DeploymentError:
                LOG.exception("could not remove network %s", network)


class DeploymentService:
    def __init__(self, state_dir: Path, engine: DockerEngine):
        self.state_dir = state_dir
        self.engine = engine
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            try:
                fd = os.open(self.state_dir / "claimed", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
            with os.fdopen(fd, "w") as file:
                file.write(str(time.time()))
            return True

    def is_claimed(self) -> bool:
        return (self.state_dir / "claimed").exists()

    def run(self, archive: bytes) -> dict:
        job_id = uuid.uuid4().hex[:12]
        (self.state_dir / "job_id").write_text(job_id)
        job_dir = self.state_dir / job_id
        repository, output = job_dir / "repository", job_dir / "output"
        repository.mkdir(parents=True, mode=0o700)
        output.mkdir(mode=0o700)
        network: str | None = None
        containers: list[str] = []
        try:
            unpack_repository(archive, repository)
            network, sandbox = self.engine.create(job_id, repository, output)
            containers.append(sandbox)
            self.engine.deploy(sandbox, job_dir / "deploy.log")
            endpoints, artifact_files = load_result(output)
            urls: dict[str, str] = {}
            for endpoint in endpoints:
                proxy, url = self.engine.expose(network, sandbox, endpoint["name"], endpoint["scheme"], endpoint["port"])
                containers.append(proxy)
                urls[endpoint["name"]] = url
            artifacts = {name: path.read_text() for name, path in artifact_files.items()}
            if "kubeconfig" in artifacts and "kubernetes" in urls:
                artifacts["kubeconfig"] = rewrite_kubeconfig(artifacts["kubeconfig"], urls["kubernetes"])
            response = {"endpoints": urls, "artifacts": artifacts}
            response_file = job_dir / "response.json"
            response_file.write_text(json.dumps(response))
            response_file.chmod(0o600)
            return response
        except Exception:
            self.engine.cleanup(network, containers)
            (self.state_dir / "failed").write_text("error deployment unsuccessful")
            raise

    def result(self) -> tuple[int, dict]:
        job_id_file = self.state_dir / "job_id"
        if not job_id_file.exists():
            previous = list(self.state_dir.glob("*/response.json"))
            if len(previous) == 1:
                return 200, json.loads(previous[0].read_text())
            return (202, {"status": "deploying"}) if self.is_claimed() else (404, {"error": "no deployment provided"})
        response_file = self.state_dir / job_id_file.read_text() / "response.json"
        if response_file.exists():
            return 200, json.loads(response_file.read_text())
        if (self.state_dir / "failed").exists():
            return 500, {"error": "error deployment unsuccessful"}
        return 202, {"status": "deploying"}


class DeploymentHandler(BaseHTTPRequestHandler):
    service: DeploymentService
    token: str | None = None

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/deploy":
            self.respond(404, {"error": "not found"})
            return
        if self.token and self.headers.get("Authorization") != f"Bearer {self.token}":
            self.respond(401, {"error": "unauthorized"})
            return
        if self.service.is_claimed():
            self.respond(409, {"error": "one already provided"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_ARCHIVE_BYTES or self.headers.get("Content-Type") not in ("application/gzip", "application/x-tar"):
            self.respond(400, {"error": "send a gzip repository tarball"})
            return
        if not self.service.claim():
            self.respond(409, {"error": "one already provided"})
            return
        try:
            archive = self.rfile.read(length)
            if len(archive) != length:
                raise DeploymentError("incomplete upload")
            self.respond(200, self.service.run(archive))
        except Exception:
            LOG.exception("deployment unsuccessful")
            self.respond(500, {"error": "error deployment unsuccessful"})

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.respond(200, {"status": "ok"})
        elif self.path == "/result":
            if self.token and self.headers.get("Authorization") != f"Bearer {self.token}":
                self.respond(401, {"error": "unauthorized"})
            else:
                status, payload = self.service.result()
                self.respond(status, payload)
        else:
            self.respond(404, {"error": "not found"})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    state = Path(os.environ.get("DEPLOY_STATE_DIR", ".deployment-state")).resolve()
    mount_specs = json.loads(os.environ.get("DEPLOY_INPUT_MOUNTS", "[]"))
    if not isinstance(mount_specs, list):
        raise SystemExit("DEPLOY_INPUT_MOUNTS must be a JSON list")
    mounts: list[tuple[Path, str]] = []
    for spec in mount_specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("source"), str) or not isinstance(spec.get("target"), str):
            raise SystemExit("invalid DEPLOY_INPUT_MOUNTS entry")
        source, target = Path(spec["source"]).resolve(), spec["target"]
        if not source.exists() or not target.startswith("/"):
            raise SystemExit("DEPLOY_INPUT_MOUNTS source must exist and target must be absolute")
        mounts.append((source, target))
    engine = DockerEngine(os.environ.get("DEPLOY_SANDBOX_IMAGE", "deployment-sandbox:latest"),
                          os.environ.get("DEPLOY_PROXY_IMAGE", "deployment-proxy:latest"),
                          os.environ.get("DEPLOY_PUBLIC_HOST", "127.0.0.1"), mounts)
    service = DeploymentService(state, engine)
    handler = type("ConfiguredDeploymentHandler", (DeploymentHandler,),
                   {"service": service, "token": os.environ.get("DEPLOY_SERVER_TOKEN")})
    host = os.environ.get("DEPLOY_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("DEPLOY_BIND_PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("DEPLOY_SERVER_TOKEN"):
        raise SystemExit("DEPLOY_SERVER_TOKEN is required for a remotely bound API")
    ThreadingHTTPServer((host, port), handler).serve_forever()


if __name__ == "__main__":
    main()
