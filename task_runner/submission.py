"""Package a submission and prepare the Docker deployment images."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def archive_submission(source: Path) -> bytes:
    if not (source / "deploy.sh").is_file():
        raise ValueError(f"missing deploy.sh in {source}")
    buffer = io.BytesIO()
    excluded = {".git", ".run-data", "__pycache__", "bin", ".venv", ".env"}
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in excluded for part in relative.parts) or path.is_symlink() or not path.is_file():
                continue
            archive.add(path, arcname=str(relative), recursive=False)
    return buffer.getvalue()


def ensure_docker_images() -> None:
    info = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"],
        capture_output=True, text=True, check=True,
    )
    if int(info.stdout.strip()) < 8 * 1024**3:
        print("warning: Docker has less than 8 GiB; increase its memory if deployment fails", file=sys.stderr)
    for name, dockerfile in (
        ("deployment-sandbox:latest", "deployment_server/Dockerfile.sandbox"),
        ("deployment-proxy:latest", "deployment_server/Dockerfile.proxy"),
    ):
        build = subprocess.run(["docker", "build", "-f", dockerfile, "-t", name, "."],
                               cwd=ROOT, capture_output=True, text=True)
        if build.returncode:
            raise RuntimeError(f"could not build {name}:\n{(build.stderr or build.stdout)[-4000:]}")
