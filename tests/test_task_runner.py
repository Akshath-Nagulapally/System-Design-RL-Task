"""The three task commands share disk state across invocations."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from loadsim import LoadSimClient, RunRecorder, TrafficResult, TrafficSample
from task_runner import cli
from task_runner.submission import archive_submission


class WorkflowHandler(BaseHTTPRequestHandler):
    values = {"welcome": "hello"}
    fail_deploy = False
    fail_seed_traffic = False
    seed_reads = 0

    def log_message(self, *_args):
        pass

    def reply(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/deploy":
            return self.reply(404)
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.fail_deploy:
            return self.reply(500, {"error": "deployment failed"})
        self.reply(200, {"endpoints": {"api": self.server.api_url,
                                       "kubernetes": "https://127.0.0.1:6443"},
                         "artifacts": {"kubeconfig": "apiVersion: v1\nclusters: []\n"}})

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if self.path.startswith("/v1/kv/"):
            key = self.path.removeprefix("/v1/kv/")
            if key == "welcome":
                self.__class__.seed_reads += 1
                if self.fail_seed_traffic and self.seed_reads > 1:
                    return self.reply(503, {"error": "unavailable"})
            return self.reply(200, {"value": self.values[key], "version": "1"}) if key in self.values else self.reply(404)
        self.reply(404)

    def do_PUT(self):
        key = self.path.removeprefix("/v1/kv/")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.values[key] = body["value"]
        self.reply(200, {"version": "1"})

    def do_DELETE(self):
        self.values.pop(self.path.removeprefix("/v1/kv/"), None)
        self.reply(204)


class TaskRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), WorkflowHandler)
        cls.server.api_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.task = cli.Task.load("distributed-kv-k3s")
        WorkflowHandler.values = {"welcome": "hello"}
        WorkflowHandler.fail_deploy = False
        WorkflowHandler.fail_seed_traffic = False
        WorkflowHandler.seed_reads = 0

    def fake_start(self, job, _task, state):
        job.update({"server_url": self.server.api_url, "server_token": "test"})
        cli._save_job(job, state)

    def fake_deploy(self):
        with patch.object(cli, "ensure_docker_images"), \
             patch.object(cli, "_start_server", self.fake_start), \
             patch.object(cli, "_cleanup_containers"), \
             patch.object(cli, "_stop_server"):
            return cli.deploy(self.task, state=self.state)

    def test_manifest_resolves_reference_and_rejects_escape(self):
        self.assertTrue((self.task.solution / "deploy.sh").is_file())
        self.assertEqual(self.task.solution, self.task.directory.parent / "solutions/KeyValueStore/solution")
        self.assertEqual(self.task.harbor, self.task.directory.parent / "harbor-task/distributed-kv-k3s")
        self.assertEqual(self.task.agent, "tasks.harbor_agents.openrouter_codex:OpenRouterCodex")
        self.assertEqual(self.task.seed.read_text().splitlines()[0], '{"key":"welcome","value":"hello"}')
        with self.assertRaisesRegex(ValueError, "unknown task"):
            cli.Task.load("../../task_runner")

    def test_generation_stages_prompt_and_finds_harbor_artifact(self):
        key_file = Path(self.temp.name) / ".env"
        key_file.write_text("OPENROUTER_API_KEY=dummy\n")

        def fake_harbor(command, **_kwargs):
            self.assertEqual(command[command.index("-m") + 1], "example/model")
            self.assertEqual(command[command.index("--agent-import-path") + 1], "example:Agent")
            staged = Path(command[command.index("-p") + 1])
            self.assertEqual((staged / "instruction.md").read_text(), self.task.prompt.read_text())
            self.assertFalse((staged / ".env").exists())
            jobs_dir = Path(command[command.index("--jobs-dir") + 1])
            job_name = command[command.index("--job-name") + 1]
            artifact = jobs_dir / job_name / "trial" / "artifacts" / "app"
            artifact.mkdir(parents=True)
            (artifact / "deploy.sh").write_text("#!/bin/sh\n")
            return SimpleNamespace(returncode=0)

        with patch.object(cli.subprocess, "run", side_effect=fake_harbor):
            output = cli.generate(self.task, state=self.state, env_file=key_file,
                                  model="example/model", agent="example:Agent")
        self.assertTrue((output / "deploy.sh").is_file())
        self.assertEqual(len(list((self.state / "generations").glob("*/generation.json"))), 1)

    def test_generate_command_hands_off_to_harbor_in_another_process(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_harbor = fake_bin / "harbor"
        fake_harbor.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            "args = sys.argv\n"
            "staged = pathlib.Path(args[args.index('-p') + 1])\n"
            "assert (staged / 'instruction.md').is_file()\n"
            "assert not (staged / '.env').exists()\n"
            "jobs = pathlib.Path(args[args.index('--jobs-dir') + 1])\n"
            "name = args[args.index('--job-name') + 1]\n"
            "output = jobs / name / 'trial' / 'artifacts' / 'app'\n"
            "output.mkdir(parents=True)\n"
            "(output / 'deploy.sh').write_text('#!/bin/sh\\n')\n"
        )
        fake_harbor.chmod(0o755)
        key_file = Path(self.temp.name) / ".env"
        key_file.write_text("OPENROUTER_API_KEY=dummy\n")
        environment = os.environ.copy()
        environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
        environment["TASK_RUNNER_STATE_DIR"] = str(self.state)
        result = subprocess.run(
            [sys.executable, "-m", "task_runner", self.task.name, "generate",
             "--env-file", str(key_file)], cwd=cli.ROOT, env=environment,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout.split("\n", 1)[1])
        self.assertTrue((Path(output["submission_path"]) / "deploy.sh").is_file())
        self.assertEqual(len(list((self.state / "generations").glob("*/generation.json"))), 1)

    def test_deploy_then_loadsim_persists_samples_and_cleans_up(self):
        job_id = self.fake_deploy()
        job = cli._load_job(job_id, self.state)
        self.assertEqual(job["status"], "deployed")
        result_path = cli._job_dir(job_id, self.state) / "deployment.json"
        self.assertEqual(json.loads(result_path.read_text())["endpoints"]["api"], self.server.api_url)
        self.assertEqual(result_path.stat().st_mode & 0o777, 0o600)
        with patch.object(cli, "_cleanup_containers") as remove, \
             patch.object(cli, "_stop_server") as stop:
            summary = cli.loadsim(self.task, state=self.state, rate=20, duration=0.05)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["successes"], 3)
        self.assertEqual(cli._load_job(job_id, self.state)["status"], "completed")
        remove.assert_called_once()
        stop.assert_called_once()
        with sqlite3.connect(self.state / "task-runner.sqlite3") as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0], "completed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM request_samples WHERE job_id = ?", (job_id,)).fetchone()[0], 3)

    def test_failed_loadsim_records_error_and_cleans_up(self):
        job_id = self.fake_deploy()
        with patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=7)), \
             patch.object(cli, "_cleanup_containers") as remove, \
             patch.object(cli, "_stop_server") as stop:
            with self.assertRaisesRegex(RuntimeError, "status 7"):
                cli.loadsim(self.task, state=self.state, duration=0.05)
        self.assertEqual(cli._load_job(job_id, self.state)["status"], "failed")
        remove.assert_called_once()
        stop.assert_called_once()
        with sqlite3.connect(self.state / "task-runner.sqlite3") as db:
            status, error = db.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(status, "failed")
        self.assertIn("status 7", error)

    def test_http_failures_are_saved_as_request_outcomes(self):
        job_id = self.fake_deploy()
        WorkflowHandler.fail_seed_traffic = True
        with patch.object(cli, "_cleanup_containers"), patch.object(cli, "_stop_server"):
            summary = cli.loadsim(self.task, state=self.state, rate=20, duration=0.1)
        self.assertEqual(summary["failures"], 2)
        with sqlite3.connect(self.state / "task-runner.sqlite3") as db:
            errors = db.execute(
                "SELECT phase, outcome, error FROM request_samples "
                "WHERE job_id = ? AND outcome = 'error'", (job_id,),
            ).fetchall()
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(phase == "get" and outcome == "error" and "503" in error
                            for phase, outcome, error in errors))

    def test_submission_archive_excludes_local_files_and_requires_deploy_script(self):
        submission = Path(self.temp.name) / "submission"
        submission.mkdir()
        (submission / "deploy.sh").write_text("#!/bin/sh\n")
        (submission / ".env").write_text("secret=local\n")
        (submission / ".git").mkdir()
        (submission / ".git" / "config").write_text("local\n")
        with tarfile.open(fileobj=io.BytesIO(archive_submission(submission)), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["deploy.sh"])
        (submission / "deploy.sh").unlink()
        with self.assertRaisesRegex(ValueError, "missing deploy.sh"):
            archive_submission(submission)

    def test_failed_deploy_records_error_and_stops_server(self):
        WorkflowHandler.fail_deploy = True
        with patch.object(cli, "ensure_docker_images"), \
             patch.object(cli, "_start_server", self.fake_start), \
             patch.object(cli, "_cleanup_containers") as remove, \
             patch.object(cli, "_stop_server") as stop:
            with self.assertRaises(httpx.HTTPStatusError):
                cli.deploy(self.task, state=self.state)
        job = json.loads(next((self.state / "jobs").glob("*/job.json")).read_text())
        self.assertEqual(job["status"], "failed")
        remove.assert_called_once()
        stop.assert_called_once()
        with sqlite3.connect(self.state / "task-runner.sqlite3") as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "failed")

    def test_interrupted_deployment_can_be_cleaned_explicitly(self):
        job_id = self.fake_deploy()
        job = cli._load_job(job_id, self.state)
        recorder = RunRecorder(self.state / "task-runner.sqlite3")
        try:
            with patch.object(cli, "_cleanup_containers") as remove, \
                 patch.object(cli, "_stop_server") as stop:
                cli.cleanup(job, state=self.state, recorder=recorder)
            self.assertEqual(cli._load_job(job_id, self.state)["status"], "cleaned")
            self.assertEqual(recorder.connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0], "cleaned")
            remove.assert_called_once()
            stop.assert_called_once()
        finally:
            recorder.close()

    def test_missing_deployment_response_records_failure_and_cleans_up(self):
        job_id = self.fake_deploy()
        (cli._job_dir(job_id, self.state) / "deployment.json").unlink()
        with patch.object(cli, "_cleanup_containers") as remove, \
             patch.object(cli, "_stop_server") as stop:
            with self.assertRaisesRegex(ValueError, "response is missing"):
                cli.loadsim(self.task, state=self.state)
        self.assertEqual(cli._load_job(job_id, self.state)["status"], "failed")
        remove.assert_called_once()
        stop.assert_called_once()

    def test_ambiguous_active_job_requires_id(self):
        first, second = self.fake_deploy(), self.fake_deploy()
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(ValueError, "found 2"):
            cli.loadsim(self.task, state=self.state)

    def test_recorder_rejects_duplicate_phase_samples(self):
        recorder = RunRecorder(self.state / "samples.sqlite3")
        try:
            recorder.create_job("a", "task", "submission")
            result = TrafficResult((TrafficSample(0, "2026-01-01T00:00:00Z", 3.0, "success"),))
            recorder.save_phase("a", "get", result)
            with self.assertRaises(sqlite3.IntegrityError):
                recorder.save_phase("a", "get", result)
            self.assertEqual(recorder.summary("a")["average_latency_ms"], 3.0)
        finally:
            recorder.close()

    def test_job_scoped_server_survives_start_call_and_stops(self):
        job = {"id": "a" * 32, "task_name": self.task.name, "status": "deploying"}
        cli._start_server(job, self.task, self.state)
        try:
            self.assertEqual(httpx.get(job["server_url"] + "/healthz").status_code, 200)
            self.assertEqual(cli._load_job(job["id"], self.state)["server_pid"], job["server_pid"])
        finally:
            cli._stop_server(job)

    def test_client_validates_json_and_secures_kubeconfig(self):
        result = {"endpoints": {"api": "http://127.0.0.1:1234", "kubernetes": "https://127.0.0.1:6443"},
                  "artifacts": {"kubeconfig": "apiVersion: v1"}}
        client = LoadSimClient.from_deployment_result(result, Path(self.temp.name) / "artifacts")
        self.assertEqual(client.api_url, result["endpoints"]["api"])
        self.assertEqual(Path(client.kubeconfig).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "missing"):
            LoadSimClient.from_deployment_result({"endpoints": {}, "artifacts": {}}, self.state)


if __name__ == "__main__":
    unittest.main()
