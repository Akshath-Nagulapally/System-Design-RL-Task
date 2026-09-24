"""The policy counts the complete planned capacity, including modules."""

import unittest

from deployment_server.resource_policy import ResourceLimits, ResourcePolicyError, validate_plan


PROVIDER = "registry.terraform.io/digitalocean/digitalocean"


def droplet(address: str, size: str) -> dict:
    return {"address": address, "mode": "managed", "provider_name": PROVIDER,
            "type": "digitalocean_droplet", "values": {"size": size}}


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.plan = {"configuration": {"provider_config": {"digitalocean": {"full_name": PROVIDER}}},
                     "planned_values": {"root_module": {"resources": [droplet("main", "small")],
                                                         "child_modules": [{"resources": [droplet("module.child.node", "small")]}]}}}
        self.sizes = {"small": (2, 2048)}

    def test_exact_budget_and_module_counting(self):
        self.assertEqual(validate_plan(self.plan, self.sizes, ResourceLimits(4, 4096)),
                         {"cpu_cores": 4, "memory_mb": 4096})

    def test_rejects_excess_and_unknown_capacity(self):
        with self.assertRaisesRegex(ResourcePolicyError, "exceeds"):
            validate_plan(self.plan, self.sizes, ResourceLimits(3, 4096))
        self.plan["planned_values"]["root_module"]["resources"][0]["values"]["size"] = "unknown"
        with self.assertRaisesRegex(ResourcePolicyError, "unknown Droplet size"):
            validate_plan(self.plan, self.sizes, ResourceLimits(4, 4096))

    def test_rejects_unmeasurable_compute_and_provisioners(self):
        self.plan["planned_values"]["root_module"]["resources"][0]["type"] = "digitalocean_kubernetes_cluster"
        with self.assertRaisesRegex(ResourcePolicyError, "unmeasurable"):
            validate_plan(self.plan, self.sizes, ResourceLimits(4, 4096))
        self.plan["planned_values"]["root_module"]["resources"][0]["type"] = "digitalocean_droplet"
        self.plan["configuration"]["root_module"] = {"resources": [{"provisioners": [{"type": "local-exec"}]}]}
        with self.assertRaisesRegex(ResourcePolicyError, "provisioners"):
            validate_plan(self.plan, self.sizes, ResourceLimits(4, 4096))

    def test_rejects_job_owned_vpc(self):
        self.plan["planned_values"]["root_module"]["resources"].append({
            "address": "digitalocean_vpc.job", "mode": "managed", "provider_name": PROVIDER,
            "type": "digitalocean_vpc", "values": {"name": "job"},
        })
        with self.assertRaisesRegex(ResourcePolicyError, "digitalocean_vpc"):
            validate_plan(self.plan, self.sizes, ResourceLimits(4, 4096))
