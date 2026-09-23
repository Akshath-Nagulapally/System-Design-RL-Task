"""Opt-in full HTTP deployment of the KeyValueStore reference submission."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from deployment_server.server import DeploymentHandler, DeploymentService, DockerEngine


@unittest.skipUnless(os.environ.get("RUN_KV_SMOKE") == "1", "set RUN_KV_SMOKE=1")
class KeyValueStoreIntegrationTests(unittest.TestCase):
    def test_repository_upload_to_api_and_kubernetes(self):
        source = Path(os.environ["KV_SOLUTION_PATH"]).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "kv.jsonl"
            seed.write_text('{"key":"hello","value":"world"}\n')
            tarball = io.BytesIO()
            with tarfile.open(fileobj=tarball, mode="w:gz") as tar:
                for path in source.rglob("*"):
                    if path.is_file() and not path.name.startswith("._") and "bin" not in path.relative_to(source).parts:
                        tar.add(path, arcname=str(path.relative_to(source)))
            engine = DockerEngine("deployment-sandbox:latest", "deployment-proxy:latest", "127.0.0.1",
                                  [(seed, "/seed/kv.jsonl")])
            state = root / "state"
            service = DeploymentService(state, engine)
            handler = type("TestHandler", (DeploymentHandler,), {"service": service, "token": None})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=1000)
                connection.request("POST", "/deploy", tarball.getvalue(), {"Content-Type": "application/gzip"})
                reply = connection.getresponse()
                body = reply.read()
                self.assertEqual(reply.status, 200, body.decode())
                response = json.loads(body)
                connection.close()
                with urlopen(response["endpoints"]["api"] + "/v1/kv/hello", timeout=10) as api_reply:
                    self.assertEqual(json.loads(api_reply.read())["value"], "world")
                kubeconfig = root / "kubeconfig"
                kubeconfig.write_text(response["artifacts"]["kubeconfig"])
                nodes = subprocess.run(["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes", "-o", "name"],
                                       capture_output=True, text=True, check=True, timeout=30)
                self.assertEqual(len(nodes.stdout.splitlines()), 3)
                another = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                another.request("POST", "/deploy", tarball.getvalue(), {"Content-Type": "application/gzip"})
                rejected = another.getresponse()
                self.assertEqual(rejected.status, 409)
                self.assertEqual(json.loads(rejected.read()), {"error": "one already provided"})
                another.close()
            finally:
                server.shutdown()
                server.server_close()
                for job in state.iterdir():
                    if job.is_dir():
                        engine.cleanup(f"deploy-{job.name}",
                                       [f"sandbox-{job.name}", f"proxy-sandbox-{job.name}-api",
                                        f"proxy-sandbox-{job.name}-kubernetes"])
