"""End-to-end runner tests without requiring a Docker daemon."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run_kv_iteration as runner


class KVHandler(BaseHTTPRequestHandler):
    values: dict[str, str] = {}
    fail_seed_traffic = False
    seed_reads = 0

    def log_message(self, *_args) -> None:
        pass

    def reply(self, status: int, data: dict | None = None) -> None:
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.reply(200, {"status": "ok"})
        elif self.path.startswith("/v1/kv/"):
            key = self.path.removeprefix("/v1/kv/")
            if key == "welcome":
                self.__class__.seed_reads += 1
                if self.fail_seed_traffic and self.seed_reads > 1:
                    self.reply(503, {"error": "unavailable"})
                    return
            if key in self.values:
                self.reply(200, {"value": self.values[key], "version": "1"})
            else:
                self.reply(404, {"error": "not found"})
        else:
            self.reply(404)

    def do_PUT(self) -> None:
        key = self.path.removeprefix("/v1/kv/")
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.values[key] = json.loads(body)["value"]
        self.reply(200, {"version": "1"})

    def do_DELETE(self) -> None:
        key = self.path.removeprefix("/v1/kv/")
        self.values.pop(key, None)
        self.reply(204)


class FakeEngine:
    instances: list["FakeEngine"] = []
    api_url = ""
    fail_deploy = False

    def __init__(self, _image, _proxy_image, _public_host, input_mounts):
        self.input_mounts = input_mounts
        self.cleaned: list[tuple[str | None, list[str]]] = []
        self.__class__.instances.append(self)

    def create(self, job_id, _repository, output):
        self.output = output
        return f"deploy-{job_id}", f"sandbox-{job_id}"

    def deploy(self, _sandbox, _log_path):
        if self.fail_deploy:
            raise RuntimeError("synthetic deployment failure")
        seed_path, target = self.input_mounts[0]
        assert target == "/seed/kv.jsonl"
        record = json.loads(seed_path.read_text().splitlines()[0])
        KVHandler.values[record["key"]] = record["value"]
        (self.output / "kubeconfig").write_text(
            "clusters:\n- cluster:\n    server: https://172.20.0.2:6443\n  name: test\n"
        )
        (self.output / "result.json").write_text(json.dumps({
            "endpoints": [{"name": "api", "scheme": "http", "port": 8080},
                          {"name": "kubernetes", "scheme": "https", "port": 6443}],
            "artifacts": [{"name": "kubeconfig", "path": "kubeconfig"}],
        }))

    def expose(self, _network, _sandbox, name, _scheme, _port):
        if name == "api":
            return "proxy-api", self.api_url
        return "proxy-kubernetes", "https://127.0.0.1:6443"

    def cleanup(self, network, containers):
        self.cleaned.append((network, containers))


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), KVHandler)
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        FakeEngine.api_url = f"http://127.0.0.1:{cls.api.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        cls.api_thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.submission = self.root / "submission"
        self.submission.mkdir()
        (self.submission / "deploy.sh").write_text("#!/bin/sh\nexit 0\n")
        self.seed = self.root / "kv.jsonl"
        self.seed.write_text('{"key":"welcome","value":"hello"}\n')
        self.database = self.root / "data" / "runs.sqlite3"
        KVHandler.values = {}
        KVHandler.fail_seed_traffic = False
        KVHandler.seed_reads = 0
        FakeEngine.instances = []
        FakeEngine.fail_deploy = False

    def args(self):
        return SimpleNamespace(submission=self.submission, seed=self.seed,
                               database=self.database, rate=20.0, duration=0.1,
                               max_in_flight=3, timeout=2.0)

    def run_fake_job(self):
        with patch.object(runner, "DockerEngine", FakeEngine), \
             patch.object(runner, "ensure_docker_images"):
            return runner.run_job(self.args())

    def test_success_persists_every_phase_and_cleans_up(self):
        self.assertEqual(self.run_fake_job(), 0)
        with sqlite3.connect(self.database) as db:
            job_id, status, error, rate, duration = db.execute(
                "SELECT id, status, error, rate_per_sec, duration_s FROM jobs"
            ).fetchone()
            self.assertEqual(status, "completed")
            self.assertIsNone(error)
            self.assertEqual((rate, duration), (20.0, 0.1))
            samples = db.execute(
                "SELECT phase, sequence, outcome, latency_ms FROM request_samples "
                "WHERE job_id = ? ORDER BY phase, sequence", (job_id,)
            ).fetchall()
            self.assertEqual(len(samples), 6)
            self.assertEqual({row[0] for row in samples}, {"get", "put", "delete"})
            self.assertTrue(all(row[2] == "success" and row[3] >= 0 for row in samples))
        self.assertEqual(len(FakeEngine.instances[0].cleaned), 1)
        network, containers = FakeEngine.instances[0].cleaned[0]
        self.assertTrue(network.startswith("deploy-"))
        self.assertEqual(len(containers), 3)

    def test_repeated_runs_get_distinct_job_ids_in_same_database(self):
        self.assertEqual(self.run_fake_job(), 0)
        self.assertEqual(self.run_fake_job(), 0)
        with sqlite3.connect(self.database) as db:
            jobs = db.execute("SELECT id, status FROM jobs").fetchall()
            self.assertEqual(len(jobs), 2)
            self.assertEqual(len({job_id for job_id, _ in jobs}), 2)
            self.assertTrue(all(status == "completed" for _, status in jobs))
            counts = db.execute("SELECT job_id, COUNT(*) FROM request_samples GROUP BY job_id").fetchall()
            self.assertEqual([count for _, count in counts], [6, 6])

    def test_failed_deployment_records_job_without_traffic(self):
        FakeEngine.fail_deploy = True
        self.assertEqual(self.run_fake_job(), 1)
        with sqlite3.connect(self.database) as db:
            status, error = db.execute("SELECT status, error FROM jobs").fetchone()
            self.assertEqual(status, "failed")
            self.assertIn("deployment failed", error)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM request_samples").fetchone()[0], 0)
        self.assertEqual(len(FakeEngine.instances[0].cleaned), 1)

    def test_http_errors_are_persisted_as_request_outcomes(self):
        KVHandler.fail_seed_traffic = True
        self.assertEqual(self.run_fake_job(), 0)
        with sqlite3.connect(self.database) as db:
            status = db.execute("SELECT status FROM jobs").fetchone()[0]
            failures = db.execute(
                "SELECT phase, outcome, error FROM request_samples WHERE outcome = 'error'"
            ).fetchall()
        self.assertEqual(status, "completed")
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(phase == "get" and "503" in error
                            for phase, _, error in failures))

    def test_missing_seed_records_failure_before_deployment(self):
        self.seed.unlink()
        self.assertEqual(self.run_fake_job(), 1)
        with sqlite3.connect(self.database) as db:
            status, error = db.execute("SELECT status, error FROM jobs").fetchone()
            self.assertEqual(status, "failed")
            self.assertIn("FileNotFoundError", error)
        self.assertEqual(FakeEngine.instances, [])

    def test_archive_excludes_local_state_and_requires_deploy_script(self):
        (self.submission / ".git").mkdir()
        (self.submission / ".git" / "secret").write_text("not part of submission")
        (self.submission / "bin").mkdir()
        (self.submission / "bin" / "old-build").write_text("old")
        data = runner.archive_submission(self.submission)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["deploy.sh"])
        (self.submission / "deploy.sh").unlink()
        with self.assertRaisesRegex(ValueError, "missing deploy.sh"):
            runner.archive_submission(self.submission)


if __name__ == "__main__":
    unittest.main()
