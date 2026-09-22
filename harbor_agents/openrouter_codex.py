"""Run Harbor's Codex agent with a full OpenRouter model slug."""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths


class OpenRouterCodex(Codex):
    """Preserve provider/model and configure Codex inside Harbor's CODEX_HOME."""

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("use a full OpenRouter model slug such as z-ai/glm-5")
        key = self._get_env("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY is missing; pass Harbor --env-file")

        agent_dir = EnvironmentPaths.agent_dir
        key_path = "/tmp/codex-openrouter.key"
        env = {"CODEX_HOME": agent_dir.as_posix()}

        # upload_file does not place the key in a shell command or CLI argument.
        with tempfile.TemporaryDirectory() as temporary:
            local_key = Path(temporary) / "openrouter.key"
            local_key.write_text(key)
            local_key.chmod(0o600)
            await environment.upload_file(local_key, key_path)

        try:
            if environment.default_user is not None:
                await self.exec_as_root(
                    environment,
                    command=f"chown {shlex.quote(str(environment.default_user))} {key_path}",
                )
            await self.exec_as_agent(
                environment,
                command=(
                    f"chmod 600 {key_path}; "
                    'cat > "$CODEX_HOME/config.toml" <<\'TOML\'\n'
                    'model_provider = "openrouter"\n'
                    '[model_providers.openrouter]\n'
                    'name = "OpenRouter"\n'
                    'base_url = "https://openrouter.ai/api/v1"\n'
                    'wire_api = "responses"\n'
                    '[model_providers.openrouter.auth]\n'
                    'command = "cat"\n'
                    f'args = ["{key_path}"]\n'
                    'TOML'
                ),
                env=env,
            )
            skills_command = self._build_register_skills_command()
            if skills_command:
                await self.exec_as_agent(environment, command=skills_command, env=env)
            mcp_command = self._build_register_mcp_servers_command()
            if mcp_command:
                await self.exec_as_agent(environment, command=mcp_command, env=env)

            flags = self.build_cli_flags()
            flags_arg = f"{flags} " if flags else ""
            await self.exec_as_agent(
                environment,
                command=(
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    "codex exec --dangerously-bypass-approvals-and-sandbox "
                    "--skip-git-repo-check "
                    f"--model {shlex.quote(self.model_name)} "
                    "--json --enable unified_exec "
                    f"{flags_arg}-- {shlex.quote(instruction)} "
                    f"2>&1 </dev/null | tee {agent_dir / self._OUTPUT_FILENAME}"
                ),
                env=env,
            )
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=f'rm -f {key_path}; rm -rf "$CODEX_HOME/tmp"',
                    env=env,
                )
            except Exception:
                self.logger.warning("could not remove the temporary OpenRouter key")
