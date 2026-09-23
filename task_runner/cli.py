"""Task-oriented command line workflow."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loadsim import RunRecorder
from deployment_server.server import DockerEngine
from .submission import archive_submission, ensure_docker_images


STATE = Path(os.environ.get("TASK_RUNNER_STATE_DIR", ROOT / ".run-data")).resolve()


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


@dataclass(frozen=True)
class Task:
    name: str
    directory: Path
    solution: Path
    prompt: Path
    loadsim: Path
    harbor: Path
    seed: Path
    model: str
    agent: str

    @classmethod
    def load(cls, name: str, root: Path = ROOT) -> "Task":
        tasks_root = (root / "task_runner" / "tasks").resolve()
        directory = (tasks_root / name).resolve()
        if not directory.is_relative_to(tasks_root) or not directory.is_dir():
            raise ValueError(f"unknown task: {name}")
        manifest = json.loads((directory / "task_manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unsupported task manifest schema_version")

        def path(field: str, *, folder: bool = False) -> Path:
            value = manifest.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"task manifest requires {field}")
            resolved = (directory / value).resolve()
            if not resolved.is_relative_to(root.resolve()) or not (resolved.is_dir() if folder else resolved.is_file()):
                raise ValueError(f"invalid task manifest path: {field}")
            return resolved

        generation = manifest.get("generation")
        if not isinstance(generation, dict) or not all(isinstance(generation.get(key), str)
                                                       for key in ("model", "agent_import_path")):
            raise ValueError("task manifest requires generation.model and agent_import_path")
        return cls(name, directory, path("solution_repository", folder=True), path("prompt"),
                   path("loadsim_script"), path("harbor_task", folder=True), path("seed"),
                   generation["model"], generation["agent_import_path"])


def _job_dir(job_id: str, state: Path = STATE) -> Path:
    if not job_id or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("invalid job ID")
    return state / "jobs" / job_id


def _load_job(job_id: str, state: Path = STATE) -> dict:
    return json.loads((_job_dir(job_id, state) / "job.json").read_text())


def _save_job(job: dict, state: Path = STATE) -> None:
    _private_json(_job_dir(job["id"], state) / "job.json", job)


def _active_job(task_name: str, state: Path = STATE) -> dict:
    matches = []
    for path in (state / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text())
        if job.get("task_name") == task_name and job.get("status") == "deployed":
            matches.append(job)
    if len(matches) != 1:
        raise ValueError(f"expected one active deployment for {task_name}, found {len(matches)}; pass --job-id")
    return matches[0]


def generate(task: Task, *, state: Path = STATE, model: str | None = None,
             agent: str | None = None, env_file: Path | None = None) -> Path:
    generation_id = uuid.uuid4().hex
    work = state / "generations" / generation_id
    work.mkdir(parents=True, mode=0o700)
    work.chmod(0o700)
    staged = work / "harbor-task"
    shutil.copytree(task.harbor, staged, ignore=shutil.ignore_patterns(".env", "__pycache__"))
    shutil.copy2(task.prompt, staged / "instruction.md")
    key_file = env_file or task.harbor / ".env"
    if not key_file.is_file():
        raise ValueError(f"OpenRouter env file missing: {key_file}")
    jobs_dir = state / "harbor-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_name = f"gen-{generation_id}"
    command = ["harbor", "run", "-p", str(staged), "-e", "docker",
               "--agent-import-path", agent or task.agent, "-m", model or task.model,
               "--env-file", str(key_file), "--disable-verification", "--artifact", "/app",
               "--jobs-dir", str(jobs_dir), "--job-name", job_name, "--n-concurrent", "1"]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    print(f"generation {generation_id}: running Harbor; log: {work / 'harbor.log'}", flush=True)
    with (work / "harbor.log").open("w") as log:
        completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"Harbor failed ({completed.returncode}); see {work / 'harbor.log'}")
    candidates = [path.parent for path in (jobs_dir / job_name).rglob("deploy.sh")
                  if path.parent.name == "app" and path.parent.parent.name == "artifacts"]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one deployable Harbor artifact, found {len(candidates)}; see {jobs_dir / job_name}")
    submission = candidates[0].resolve()
    _private_json(work / "generation.json", {"id": generation_id, "task_name": task.name,
                                               "model": model or task.model,
                                               "agent": agent or task.agent,
                                               "submission_path": str(submission)})
    print(json.dumps({"generation_id": generation_id, "submission_path": str(submission)}, indent=2))
    return submission


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(job: dict, task: Task, state: Path = STATE) -> None:
    directory = _job_dir(job["id"], state)
    port = _unused_port()
    token = secrets.token_urlsafe(32)
    server_state = directory / "server"
    server_state.mkdir(parents=True, mode=0o700)
    environment = os.environ.copy()
    environment.update({"DEPLOY_STATE_DIR": str(server_state), "DEPLOY_BIND_HOST": "127.0.0.1",
                        "DEPLOY_BIND_PORT": str(port), "DEPLOY_SERVER_TOKEN": token,
                        "DEPLOY_INPUT_MOUNTS": json.dumps([{"source": str(task.seed),
                                                              "target": "/seed/kv.jsonl"}])})
    log = (directory / "server.log").open("w")
    try:
        process = subprocess.Popen([sys.executable, "-m", "task_runner.server_process", job["id"]],
                                   cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    finally:
        log.close()
    job.update({"server_pid": process.pid, "server_url": f"http://127.0.0.1:{port}",
                "server_token": token})
    _save_job(job, state)
    deadline = time.monotonic() + 10
    with httpx.Client(timeout=1) as http:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"deployment server exited; see {directory / 'server.log'}")
            try:
                if http.get(job["server_url"] + "/healthz").status_code == 200:
                    # The server deliberately outlives this command. Suppress
                    # Popen's warning when its Python handle is discarded.
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", ResourceWarning)
                        del process
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError(f"deployment server did not start; see {directory / 'server.log'}")


def _stop_server(job: dict) -> None:
    pid = job.get("server_pid")
    if not isinstance(pid, int):
        return
    # Check the command line before signaling a process whose PID may have been reused.
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True,
                             capture_output=True).stdout
    if "task_runner.server_process" in command and job["id"] in command:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _cleanup_containers(job: dict, state: Path = STATE) -> None:
    id_file = _job_dir(job["id"], state) / "server" / "job_id"
    if not id_file.is_file():
        return
    deployment_id = id_file.read_text().strip()
    if not deployment_id or any(c not in "0123456789abcdef" for c in deployment_id):
        raise ValueError("invalid deployment ID in server state")
    engine = DockerEngine("deployment-sandbox:latest", "deployment-proxy:latest", "127.0.0.1")
    proxies = engine.command("ps", "-a", "--filter", f"name=proxy-sandbox-{deployment_id}-",
                             "--format", "{{.Names}}").splitlines()
    engine.cleanup(f"deploy-{deployment_id}", [f"sandbox-{deployment_id}", *proxies])


def cleanup(job: dict, *, state: Path = STATE, recorder: RunRecorder | None = None) -> None:
    try:
        _cleanup_containers(job, state)
    finally:
        _stop_server(job)
    if job["status"] in ("deploying", "deployed", "running"):
        job["status"] = "cleaned"
        _save_job(job, state)
        if recorder:
            recorder.update_job(job["id"], "cleaned")


def deploy(task: Task, submission: Path | None = None, *, state: Path = STATE) -> str:
    submission = (submission or task.solution).resolve()
    archive = archive_submission(submission)
    job = {"id": uuid.uuid4().hex, "task_name": task.name,
           "submission_path": str(submission), "status": "deploying"}
    recorder = RunRecorder(state / "task-runner.sqlite3")
    recorder.create_job(job["id"], task.name, str(submission))
    _save_job(job, state)
    try:
        ensure_docker_images()
        _start_server(job, task, state)
        with httpx.Client(timeout=960) as http:
            response = http.post(job["server_url"] + "/deploy", content=archive,
                                 headers={"Content-Type": "application/gzip",
                                          "Authorization": f"Bearer {job['server_token']}"})
            response.raise_for_status()
            result = response.json()
        # Validate the handoff before considering the deployment usable.
        from loadsim import LoadSimClient
        LoadSimClient.from_deployment_result(result, _job_dir(job["id"], state))
        _private_json(_job_dir(job["id"], state) / "deployment.json", result)
        job["status"] = "deployed"
        _save_job(job, state)
        recorder.update_job(job["id"], "deployed", api_url=result["endpoints"]["api"])
        print(json.dumps({"job_id": job["id"], "endpoints": result["endpoints"]}, indent=2))
        return job["id"]
    except BaseException as exc:
        job["status"] = "failed"
        _save_job(job, state)
        recorder.update_job(job["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        cleanup(job, state=state)
        raise
    finally:
        recorder.close()


def loadsim(task: Task, *, job_id: str | None = None, state: Path = STATE,
            rate: float = 10, duration: float = 5, max_in_flight: int = 20,
            timeout: float = 5) -> dict:
    job = _load_job(job_id, state) if job_id else _active_job(task.name, state)
    if job["task_name"] != task.name or job["status"] != "deployed":
        raise ValueError("job is not an active deployment for this task")
    if (any(not math.isfinite(value) or value <= 0 for value in (rate, duration, timeout))
            or max_in_flight <= 0):
        raise ValueError("traffic settings must be finite and positive")
    directory = _job_dir(job["id"], state)
    result_file = directory / "deployment.json"
    recorder = RunRecorder(state / "task-runner.sqlite3")
    try:
        if not result_file.is_file():
            raise ValueError("deployment response is missing")
        job["status"] = "running"
        _save_job(job, state)
        recorder.update_job(job["id"], "running")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
        environment.update({"TASK_DEPLOYMENT_JSON": str(result_file), "TASK_JOB_ID": job["id"],
                            "TASK_DB_PATH": str(state / "task-runner.sqlite3"),
                            "TASK_SEED_PATH": str(task.seed), "TASK_RATE": str(rate),
                            "TASK_DURATION": str(duration), "TASK_MAX_IN_FLIGHT": str(max_in_flight),
                            "TASK_TIMEOUT": str(timeout)})
        # run_path keeps the task directory off sys.path, where loadsim.py would
        # otherwise shadow the installed loadsim package.
        completed = subprocess.run(
            [sys.executable, "-c", "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')",
             str(task.loadsim)], cwd=ROOT, env=environment,
        )
        if completed.returncode:
            raise RuntimeError(f"loadsim script exited with status {completed.returncode}")
        job["status"] = "completed"
        _save_job(job, state)
        recorder.update_job(job["id"], "completed")
        summary = recorder.summary(job["id"])
        print(json.dumps(summary, indent=2))
        return summary
    except BaseException as exc:
        job["status"] = "failed"
        _save_job(job, state)
        recorder.update_job(job["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        cleanup(job, state=state, recorder=recorder)
        recorder.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="task name under task_runner/tasks/")
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", aliases=["generate_agent_solution"])
    generate_parser.add_argument("--model")
    generate_parser.add_argument("--agent")
    generate_parser.add_argument("--env-file", type=Path)
    deploy_parser = commands.add_parser("deploy")
    deploy_parser.add_argument("submission", nargs="?", type=Path)
    loadsim_parser = commands.add_parser("loadsim")
    loadsim_parser.add_argument("--job-id")
    loadsim_parser.add_argument("--rate", type=float, default=10)
    loadsim_parser.add_argument("--duration", type=float, default=5)
    loadsim_parser.add_argument("--max-in-flight", type=int, default=20)
    loadsim_parser.add_argument("--timeout", type=float, default=5)
    cleanup_parser = commands.add_parser("cleanup")
    cleanup_parser.add_argument("job_id")
    args = parser.parse_args(argv)
    try:
        task = Task.load(args.task)
        if args.command in ("generate", "generate_agent_solution"):
            generate(task, model=args.model, agent=args.agent, env_file=args.env_file)
        elif args.command == "deploy":
            deploy(task, args.submission)
        elif args.command == "loadsim":
            loadsim(task, job_id=args.job_id, rate=args.rate, duration=args.duration,
                    max_in_flight=args.max_in_flight, timeout=args.timeout)
        else:
            job = _load_job(args.job_id)
            if job["task_name"] != task.name:
                raise ValueError("job belongs to another task")
            recorder = RunRecorder(STATE / "task-runner.sqlite3")
            try:
                cleanup(job, recorder=recorder)
            finally:
                recorder.close()
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"task-runner: {exc}", file=sys.stderr)
        return 1
    return 0
