"""Opt-in real Docker check for the generic sandbox and public TCP relay."""

import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from server import DeploymentService, DockerEngine


@unittest.skipUnless(os.environ.get("RUN_DOCKER_SMOKE") == "1", "set RUN_DOCKER_SMOKE=1")
class DockerSmokeTests(unittest.TestCase):
    def test_deploy_and_reach_public_endpoint(self):
        script = """#!/bin/bash
set -euo pipefail
mkdir -p /tmp/site
printf 'ready' > /tmp/site/healthz
httpd -p 0.0.0.0:18080 -h /tmp/site
printf '{"endpoints":[{"name":"api","scheme":"http","port":18080}]}' > "$DEPLOY_OUTPUT_DIR/result.json"
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "deploy.sh").write_text(script)
            archive = root / "submission.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source / "deploy.sh", arcname="deploy.sh")
            engine = DockerEngine("deployment-sandbox:latest", "deployment-proxy:latest", "127.0.0.1", None)
            service = DeploymentService(root / "state", engine)
            try:
                response = service.run(archive.read_bytes())
                with urlopen(response["endpoints"]["api"] + "/healthz", timeout=5) as reply:
                    self.assertEqual(reply.read(), b"ready")
                self.assertEqual(response["artifacts"], {})
            finally:
                for job in (root / "state").iterdir():
                    if job.is_dir():
                        engine.cleanup(f"deploy-{job.name}",
                                       [f"sandbox-{job.name}", f"proxy-sandbox-{job.name}-api"])


if __name__ == "__main__":
    unittest.main()
