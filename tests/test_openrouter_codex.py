"""Check the Harbor-specific OpenRouter adapter without making model calls."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

if importlib.util.find_spec("harbor") is not None:
    from harbor_agents.openrouter_codex import OpenRouterCodex
else:
    OpenRouterCodex = None


class FakeEnvironment:
    default_user = None

    async def upload_file(self, source, target):
        assert self.root_started
        self.uploaded_path = target
        self.uploaded_contents = Path(source).read_text()


@unittest.skipIf(OpenRouterCodex is None, "install Harbor to test its agent adapter")
class OpenRouterCodexTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_slug_provider_config_and_key_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = OpenRouterCodex(
                logs_dir=Path(temporary), model_name="z-ai/glm-5",
                extra_env={"OPENROUTER_API_KEY": "test-key-only"},
            )
            agent.exec_as_agent = AsyncMock()
            agent.exec_as_root = AsyncMock(side_effect=lambda *args, **kwargs: setattr(
                environment, "root_started", True))
            environment = FakeEnvironment()
            await agent.run("say hello", environment, None)

        commands = [call.kwargs["command"] for call in agent.exec_as_agent.call_args_list]
        self.assertEqual(environment.uploaded_path, "/opt/codex-openrouter.key")
        self.assertEqual(environment.uploaded_contents, "test-key-only")
        self.assertTrue(environment.root_started)
        startup = agent.exec_as_root.call_args.kwargs["command"]
        self.assertIn("dockerd-entrypoint.sh", startup)
        self.assertIn("docker info", startup)
        self.assertIn("openrc default", startup)
        self.assertIn('model_provider = "openrouter"', commands[0])
        self.assertIn('wire_api = "responses"', commands[0])
        self.assertIn("--model z-ai/glm-5", commands[1])
        self.assertIn("rm -f /opt/codex-openrouter.key", commands[-1])
        self.assertTrue(all("test-key-only" not in command for command in commands))
        self.assertTrue(all("test-key-only" not in str(call.kwargs.get("env", {}))
                            for call in agent.exec_as_agent.call_args_list))

    async def test_rejects_missing_key_and_incomplete_model_slug(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = OpenRouterCodex(logs_dir=Path(temporary), model_name="z-ai/glm-5")
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY is missing"):
                await agent.run("hello", FakeEnvironment(), None)
            agent.model_name = "glm-5"
            with self.assertRaisesRegex(ValueError, "full OpenRouter model slug"):
                await agent.run("hello", FakeEnvironment(), None)


if __name__ == "__main__":
    unittest.main()
