#!/usr/bin/env python3
"""Deploy one KV submission, run baseline traffic, and persist its samples."""

from __future__ import annotations

import argparse
import asyncio
import io
import itertools
import json
import sqlite3
import subprocess
import sys
import tarfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loadsim import LoadSimClient, TrafficResult  # noqa: E402
from server import DeploymentHandler, DeploymentService, DockerEngine  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            submission_path TEXT NOT NULL,
            seed_path TEXT NOT NULL,
            rate_per_sec REAL NOT NULL,
            duration_s REAL NOT NULL,
            max_in_flight INTEGER NOT NULL,
            timeout_s REAL NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            api_url TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS request_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            phase TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            latency_ms REAL,
            outcome TEXT NOT NULL,
            error TEXT,
            UNIQUE(job_id, phase, sequence)
        );
        CREATE INDEX IF NOT EXISTS request_samples_job_id ON request_samples(job_id);
    """)
    return connection


def save_phase(connection: sqlite3.Connection, job_id: str, phase: str, result: TrafficResult) -> None:
    with connection:
        connection.executemany(
            """INSERT INTO request_samples
               (job_id, phase, sequence, timestamp, latency_ms, outcome, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ((job_id, phase, sample.sequence, sample.timestamp, sample.latency_ms,
              sample.outcome, sample.error) for sample in result.samples),
        )


def archive_submission(source: Path) -> bytes:
    if not (source / "deploy.sh").is_file():
        raise ValueError(f"missing deploy.sh in {source}")
    buffer = io.BytesIO()
    excluded = {".git", ".run-data", "__pycache__", "bin", ".venv"}
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
        ("deployment-sandbox:latest", "Dockerfile.sandbox"),
        ("deployment-proxy:latest", "Dockerfile.proxy"),
    ):
        exists = subprocess.run(["docker", "image", "inspect", name],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if exists.returncode:
            subprocess.run(["docker", "build", "-f", dockerfile, "-t", name, "."],
                           cwd=ROOT, check=True)


def first_seed_record(path: Path) -> tuple[str, str]:
    with path.open(encoding="utf-8") as seed:
        for line in seed:
            if line.strip():
                record = json.loads(line)
                return record["key"], record["value"]
    raise ValueError(f"seed file is empty: {path}")


async def run_traffic(
    client: LoadSimClient,
    job_id: str,
    seed_record: tuple[str, str],
    *,
    rate: float,
    duration: float,
    max_in_flight: int,
    timeout: float,
    on_phase: Callable[[str, TrafficResult], None],
) -> None:
    seed_key, seed_value = seed_record
    async with httpx.AsyncClient(base_url=client.api_url, timeout=timeout) as http:
        health = await http.get("/healthz")
        health.raise_for_status()
        seeded = await http.get(f"/v1/kv/{seed_key}")
        seeded.raise_for_status()
        if seeded.json().get("value") != seed_value:
            raise RuntimeError("deployed service did not import the supplied seed")

        probe_key = f"e2e-{job_id[:12]}-probe"
        probe = await http.put(f"/v1/kv/{probe_key}", json={"value": "probe"})
        probe.raise_for_status()
        if not probe.json().get("version"):
            raise RuntimeError("PUT did not return a version")
        read_back = await http.get(f"/v1/kv/{probe_key}")
        read_back.raise_for_status()
        if read_back.json().get("value") != "probe":
            raise RuntimeError("GET did not return the written value")
        deleted = await http.delete(f"/v1/kv/{probe_key}")
        if deleted.status_code != 204:
            raise RuntimeError(f"DELETE returned {deleted.status_code}, expected 204")
        missing = await http.get(f"/v1/kv/{probe_key}")
        if missing.status_code != 404:
            raise RuntimeError(f"deleted key returned {missing.status_code}, expected 404")

        async def read_seed() -> None:
            response = await http.get(f"/v1/kv/{seed_key}")
            response.raise_for_status()
            if response.json().get("value") != seed_value:
                raise RuntimeError("seed value changed during read traffic")

        keys: list[str] = []
        numbers = itertools.count()

        async def write_unique() -> None:
            key = f"e2e-{job_id[:12]}-{next(numbers)}"
            response = await http.put(f"/v1/kv/{key}", json={"value": "traffic"})
            response.raise_for_status()
            if not response.json().get("version"):
                raise RuntimeError("PUT did not return a version")
            keys.append(key)

        async def delete_written() -> None:
            key = next(delete_keys)
            response = await http.delete(f"/v1/kv/{key}")
            if response.status_code != 204:
                raise RuntimeError(f"DELETE returned {response.status_code}, expected 204")

        settings = dict(rate_per_sec=rate, duration_s=duration,
                        max_in_flight=max_in_flight, timeout_s=timeout)
        result = await client.atraffic(read_seed, **settings)
        on_phase("get", result)
        result = await client.atraffic(write_unique, **settings)
        on_phase("put", result)
        if not keys:
            raise RuntimeError("no PUT request succeeded; cannot run DELETE traffic")
        delete_keys = itertools.cycle(keys)
        result = await client.atraffic(delete_written, **settings)
        on_phase("delete", result)


def summary(connection: sqlite3.Connection, job_id: str) -> dict:
    count, successes, failures, dropped, average = connection.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome IN ('error', 'timeout') THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome = 'dropped' THEN 1 ELSE 0 END),
                  AVG(latency_ms)
           FROM request_samples WHERE job_id = ?""", (job_id,)
    ).fetchone()
    return {"job_id": job_id, "requests": count, "successes": successes or 0,
            "failures": failures or 0, "dropped": dropped or 0,
            "average_latency_ms": average}


def run_job(args: argparse.Namespace) -> int:
    submission = args.submission.resolve()
    seed = args.seed.resolve()
    job_id = uuid.uuid4().hex
    connection = open_database(args.database.resolve())
    with connection:
        connection.execute(
            """INSERT INTO jobs
               (id, submission_path, seed_path, rate_per_sec, duration_s,
                max_in_flight, timeout_s, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'deploying', ?)""",
            (job_id, str(submission), str(seed), args.rate, args.duration,
             args.max_in_flight, args.timeout, utc_now()),
        )
    service: DeploymentService | None = None
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    deployment: dict | None = None
    try:
        seed_record = first_seed_record(seed)
        payload = archive_submission(submission)
        ensure_docker_images()
        state_dir = args.database.resolve().parent / "deployments" / job_id
        engine = DockerEngine("deployment-sandbox:latest", "deployment-proxy:latest",
                              "127.0.0.1", [(seed, "/seed/kv.jsonl")])
        service = DeploymentService(state_dir, engine)
        handler = type("JobDeploymentHandler", (DeploymentHandler,),
                       {"service": service, "token": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"job {job_id}: deploying {submission}", flush=True)
        with httpx.Client(timeout=960) as http:
            reply = http.post(url + "/deploy", content=payload,
                              headers={"Content-Type": "application/gzip"})
            if reply.status_code != 200:
                raise RuntimeError(f"deployment failed ({reply.status_code}): {reply.text}")
            deployment = reply.json()
        api_url = deployment["endpoints"]["api"]
        kubernetes_url = deployment["endpoints"]["kubernetes"]
        kubeconfig = deployment["artifacts"]["kubeconfig"]
        config_path = state_dir / "kubeconfig"
        config_path.write_text(kubeconfig)
        config_path.chmod(0o600)
        with connection:
            connection.execute("UPDATE jobs SET status = 'running', api_url = ? WHERE id = ?",
                               (api_url, job_id))
        load_client = LoadSimClient(config_path, kubernetes_url, api_url)
        print(f"job {job_id}: running baseline traffic against {api_url}", flush=True)
        asyncio.run(run_traffic(
            load_client, job_id, seed_record, rate=args.rate, duration=args.duration,
            max_in_flight=args.max_in_flight, timeout=args.timeout,
            on_phase=lambda phase, result: save_phase(connection, job_id, phase, result),
        ))
        with connection:
            connection.execute("UPDATE jobs SET status = 'completed', finished_at = ? WHERE id = ?",
                               (utc_now(), job_id))
        print(json.dumps(summary(connection, job_id), indent=2))
        return 0
    except BaseException as exc:
        with connection:
            connection.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                               (utc_now(), f"{type(exc).__name__}: {exc}", job_id))
        print(f"job {job_id} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if service is not None:
            deployment_id_file = service.state_dir / "job_id"
            if deployment_id_file.exists():
                deployment_id = deployment_id_file.read_text().strip()
                response_file = service.state_dir / deployment_id / "response.json"
                if response_file.exists():
                    saved_response = json.loads(response_file.read_text())
                    sandbox = f"sandbox-{deployment_id}"
                    proxies = [f"proxy-{sandbox}-{name}"[:63]
                               for name in saved_response["endpoints"]]
                    service.engine.cleanup(f"deploy-{deployment_id}", [sandbox, *proxies])
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission", type=Path, help="directory with deploy.sh at its root")
    parser.add_argument("--seed", type=Path,
                        default=ROOT / "tasks/distributed-kv-k3s/environment/seed/kv.jsonl")
    parser.add_argument("--database", type=Path, default=ROOT / ".run-data/kv-runs.sqlite3")
    parser.add_argument("--rate", type=float, default=10.0, help="scheduled requests per second")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per traffic phase")
    parser.add_argument("--max-in-flight", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds per request")
    args = parser.parse_args()
    if args.rate <= 0 or args.duration <= 0 or args.max_in_flight <= 0 or args.timeout <= 0:
        parser.error("rate, duration, max-in-flight, and timeout must be positive")
    return run_job(args)


if __name__ == "__main__":
    raise SystemExit(main())
