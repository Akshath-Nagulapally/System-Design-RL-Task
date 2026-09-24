"""Opt-in end-to-end test of separate task-runner commands and real Docker."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
TASK = "distributed-kv-k3s"


@unittest.skipUnless(os.environ.get("RUN_DOCKER_SMOKE") == "1", "set RUN_DOCKER_SMOKE=1")
class TaskRunnerDockerTests(unittest.TestCase):
    def test_reference_deploy_then_loadsim_across_cli_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            environment = os.environ.copy()
            environment["TASK_RUNNER_STATE_DIR"] = str(state)
            environment["TASK_DEPLOY_BACKEND"] = "docker"
            command = [sys.executable, "-m", "task_runner", TASK]
            job_id: str | None = None
            deployment_id: str | None = None

            try:
                deployed = subprocess.run(command + ["deploy", "task_runner/resources/solutions/KeyValueStore/solution"], cwd=ROOT, env=environment,
                                          capture_output=True, text=True, timeout=1200)
                self.assertEqual(deployed.returncode, 0, deployed.stdout + deployed.stderr)
                handoff = json.loads(deployed.stdout)
                job_id = handoff["job_id"]
                job_dir = state / "jobs" / job_id
                deployment_id = (job_dir / "server" / "job_id").read_text().strip()
                result = json.loads((job_dir / "deployment.json").read_text())
                self.assertEqual(handoff["endpoints"], result["endpoints"])
                self.assertTrue(result["artifacts"]["kubeconfig"])
                self.assertEqual((job_dir / "deployment.json").stat().st_mode & 0o777, 0o600)
                with urlopen(result["endpoints"]["api"] + "/healthz", timeout=5) as reply:
                    self.assertEqual(reply.status, 200)
                self.assertEqual(subprocess.run(["docker", "inspect", f"sandbox-{deployment_id}"],
                                                capture_output=True).returncode, 0)
                sandbox = f"sandbox-{deployment_id}"
                version = subprocess.run(
                    ["docker", "exec", sandbox, "kubectl", "--kubeconfig",
                     "/deploy-output/kubeconfig", "get", "--raw", "/version"],
                    capture_output=True, text=True, timeout=15, check=True,
                )
                self.assertEqual(json.loads(version.stdout)["gitVersion"], "v1.35.5+k3s1")
                etcd_image = subprocess.run(
                    ["docker", "exec", sandbox, "kubectl", "--kubeconfig",
                     "/deploy-output/kubeconfig", "-n", "kvstore", "get", "statefulset", "etcd",
                     "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
                    capture_output=True, text=True, timeout=15, check=True,
                )
                self.assertEqual(etcd_image.stdout, "gcr.io/etcd-development/etcd:v3.6.14")

                loaded = subprocess.run(
                    command + ["loadsim", "--job-id", job_id, "--rate", "20",
                               "--duration", "0.25", "--timeout", "5"],
                    cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
                summary = json.loads(loaded.stdout)
                self.assertEqual(summary["job_id"], job_id)
                self.assertEqual(summary["requests"], 15)
                self.assertEqual(summary["successes"], 15)
                self.assertEqual(summary["failures"], 0)
                with sqlite3.connect(state / "task-runner.sqlite3") as db:
                    self.assertEqual(db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
                                     "completed")
                    phases = db.execute(
                        "SELECT phase, COUNT(*) FROM request_samples WHERE job_id = ? GROUP BY phase",
                        (job_id,),
                    ).fetchall()
                self.assertEqual(dict(phases), {"get": 5, "put": 5, "delete": 5})
                self.assertNotEqual(subprocess.run(["docker", "inspect", f"sandbox-{deployment_id}"],
                                                  capture_output=True).returncode, 0)
                self.assertNotEqual(subprocess.run(["docker", "network", "inspect", f"deploy-{deployment_id}"],
                                                  capture_output=True).returncode, 0)
            finally:
                for directory in (state / "jobs").glob("*"):
                    candidate_id = directory.name
                    job_file = directory / "job.json"
                    id_file = directory / "server" / "job_id"
                    candidate_deployment = id_file.read_text().strip() if id_file.exists() else None
                    status = json.loads(job_file.read_text())["status"] if job_file.exists() else None
                    if status in ("deploying", "deployed", "running") or (
                        candidate_deployment and subprocess.run(
                            ["docker", "inspect", f"sandbox-{candidate_deployment}"],
                            capture_output=True,
                        ).returncode == 0
                    ):
                        subprocess.run(command + ["cleanup", candidate_id], cwd=ROOT,
                                       env=environment, capture_output=True, text=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
