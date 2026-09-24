"""Crash requests must remove only live Droplets owned by this deployment."""

import json
import tempfile
import unittest
from pathlib import Path

from deployment_server.digitalocean import DigitalOceanDeploymentService, _endpoint_ips


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
        with self.assertRaisesRegex(ValueError, "no Droplets remain"):
            self.service.crash(1)
        self.assertNotIn(3, self.service.api.deleted)

    def test_invalid_count_changes_nothing(self):
        for count in (0, -1, True, 3):
            with self.assertRaises(ValueError):
                self.service.crash(count)
        self.assertEqual(self.service.api.deleted, [])

    def test_percent_rounds_down_and_stays_in_project(self):
        self.service.api.project.add("do:droplet:3")
        result = self.service.crash(percent=50)
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(result["remaining"], 2)
        self.assertIn(self.service.api.deleted[0], {1, 2, 3})

    def test_percent_has_minimum_one_and_rejects_empty_deployment(self):
        self.service.crash(percent=1)
        self.service.crash(percent=50)
        with self.assertRaisesRegex(ValueError, "no Droplets remain"):
            self.service.crash(percent=50)

    def test_invalid_percent_changes_nothing(self):
        for percent in (0, -1, 101, True, 50.0):
            with self.assertRaises(ValueError):
                self.service.crash(percent=percent)
        with self.assertRaisesRegex(ValueError, "either crash count or percent"):
            self.service.crash(1, percent=50)
        self.assertEqual(self.service.api.deleted, [])

    def test_only_job_load_balancer_ip_is_an_allowed_endpoint(self):
        outputs = {"droplet_ips": {"value": ["192.0.2.10"]}}
        state = {"values": {"root_module": {"resources": [
            {"type": "digitalocean_loadbalancer", "values": {
                "urn": "do:loadbalancer:owned", "ip": "192.0.2.20"}},
            {"type": "digitalocean_loadbalancer", "values": {
                "urn": "do:loadbalancer:foreign", "ip": "192.0.2.30"}},
        ]}}}
        self.assertEqual(_endpoint_ips(outputs, state, "project-a", {"do:loadbalancer:owned"}),
                         {"192.0.2.10", "192.0.2.20"})
