"""Crash requests must remove only live Droplets owned by this deployment."""

import json
import tempfile
import unittest
from pathlib import Path

from deployment_server.digitalocean import DigitalOceanDeploymentService


class FakeDigitalOceanAPI:
    def __init__(self):
        self.live = {1, 2, 3}
        self.project = {"do:droplet:1", "do:droplet:2"}
        self.deleted = []

    def project_droplet_urns(self, project_id):
        assert project_id == "project-a"
        return self.project

    def delete_droplet(self, droplet_id):
        self.deleted.append(droplet_id)
        self.live.remove(droplet_id)

    def droplet_exists(self, droplet_id):
        return droplet_id in self.live


class CrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        (state / "job_id").write_text("job-a")
        job = state / "job-a"
        job.mkdir()
        (job / "response.json").write_text("{}")
        (job / "project_id").write_text("project-a")
        (job / "droplets.json").write_text(json.dumps([
            {"id": index, "urn": f"do:droplet:{index}", "name": f"node-{index}"}
            for index in (1, 2, 3)
        ]))
        self.service = DigitalOceanDeploymentService(state, "unused", "nyc3", job / "seed")
        self.service.api = FakeDigitalOceanAPI()

    def test_each_request_crashes_one_distinct_project_droplet(self):
        first = self.service.crash(1)
        second = self.service.crash(1)
        self.assertEqual(len(first["removed"]), 1)
        self.assertEqual(len(second["removed"]), 1)
        self.assertEqual(set(self.service.api.deleted), {1, 2})
        self.assertEqual(first["remaining"], 1)
        self.assertEqual(second["remaining"], 0)
        with self.assertRaisesRegex(ValueError, "only 0"):
            self.service.crash(1)
        self.assertNotIn(3, self.service.api.deleted)

    def test_invalid_count_changes_nothing(self):
        for count in (0, -1, True, 3):
            with self.assertRaises(ValueError):
                self.service.crash(count)
        self.assertEqual(self.service.api.deleted, [])
