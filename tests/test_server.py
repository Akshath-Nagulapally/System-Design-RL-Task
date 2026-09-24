import io
import json
import tarfile
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from deployment_server.server import DeploymentError, DeploymentHandler, DeploymentService, load_result, rewrite_kubeconfig, unpack_repository


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeEngine:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def create(self, job_id, repository, output):
        self.output = output
        self.calls += 1
        return "network", "sandbox"

    def deploy(self, sandbox, log_path):
        self.started.set()
        if not self.release.wait(5):
            raise DeploymentError("test timed out")
        (self.output / "result.json").write_text(json.dumps({
            "endpoints": [{"name": "api", "scheme": "http", "port": 8080},
                          {"name": "kubernetes", "scheme": "https", "port": 6443}],
            "artifacts": [{"name": "kubeconfig", "path": "kubeconfig"}],
        }))
        (self.output / "kubeconfig").write_text(
            "clusters:\n- cluster:\n    server: https://172.20.0.2:6443\n  name: test\n"
        )

    def expose(self, network, sandbox, name, scheme, port):
        return f"proxy-{name}", f"{scheme}://example.test:{port}"

    def cleanup(self, network, containers):
        pass


class HandoffTests(unittest.TestCase):
    def test_archive_extraction_accepts_wrapped_folder_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unpack_repository(archive({"solution/deploy.sh": b"#!/bin/sh\nexit 0\n"}), root)
            self.assertTrue((root / "deploy.sh").is_file())
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DeploymentError):
                unpack_repository(archive({"../deploy.sh": b"bad"}), Path(temporary))

    def test_result_rejects_missing_artifact_and_duplicate_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "result.json").write_text(json.dumps({
                "endpoints": [{"name": "api", "scheme": "http", "port": 8080},
                              {"name": "api", "scheme": "http", "port": 8081}],
            }))
            with self.assertRaises(DeploymentError):
                load_result(root)
            (root / "result.json").write_text(json.dumps({
                "endpoints": [{"name": "api", "scheme": "http", "port": 8080}],
                "artifacts": [{"name": "kubeconfig", "path": "missing"}],
            }))
            with self.assertRaises(DeploymentError):
                load_result(root)

    def test_kubeconfig_uses_public_url_and_original_tls_identity(self):
        config = "clusters:\n- cluster:\n    server: https://172.20.0.2:6443\n  name: test\n"
        changed = rewrite_kubeconfig(config, "https://public.example:49152")
        self.assertIn("server: https://public.example:49152", changed)
        self.assertIn("tls-server-name: 172.20.0.2", changed)

    def test_only_one_upload_even_while_first_is_running_and_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = FakeEngine()
            service = DeploymentService(Path(temporary), engine)
            handler = type("TestHandler", (DeploymentHandler,), {"service": service, "token": None})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            body = archive({"deploy.sh": b"#!/bin/sh\nexit 0\n"})
            results = []

            def post():
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("POST", "/deploy", body, {"Content-Type": "application/gzip"})
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result

            def get_result():
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("GET", "/result")
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result

            first = threading.Thread(target=lambda: results.append(post()))
            try:
                first.start()
                self.assertTrue(engine.started.wait(3))
                self.assertEqual(post(), (409, {"error": "one already provided"}))
                self.assertEqual(get_result(), (202, {"status": "deploying"}))
                engine.release.set()
                first.join(5)
                self.assertEqual(results[0][0], 200)
                self.assertEqual(results[0][1]["endpoints"]["api"], "http://example.test:8080")
                self.assertIn("tls-server-name: 172.20.0.2", results[0][1]["artifacts"]["kubeconfig"])
                self.assertEqual(engine.calls, 1)
                self.assertEqual(get_result(), results[0])
                self.assertFalse(DeploymentService(Path(temporary), FakeEngine()).claim())
            finally:
                engine.release.set()
                server.shutdown()
                server.server_close()

    def test_failed_upload_returns_error_and_still_uses_only_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = DeploymentService(Path(temporary), FakeEngine())
            handler = type("TestHandler", (DeploymentHandler,), {"service": service, "token": None})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            body = archive({"README.md": b"missing deploy script"})
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("POST", "/deploy", body, {"Content-Type": "application/gzip"})
                response = connection.getresponse()
                self.assertEqual(response.status, 500)
                self.assertEqual(json.loads(response.read()), {"error": "error deployment unsuccessful"})
                connection.close()
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("POST", "/deploy", body, {"Content-Type": "application/gzip"})
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read()), {"error": "one already provided"})
                connection.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
