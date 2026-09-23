# Distributed KV Harbor task

This Harbor task asks a coding agent to implement the assignment copied from `solutions/KeyValueStore/spec.md`. The agent sees `/app/spec.md` and a small read-only seed at `/seed/kv.jsonl`. Its deliverable is an executable `/app/deploy.sh` plus everything required for a fresh deployment.

The intended harness is Harbor's Codex CLI agent. From the repository root, a future run can use:

```sh
harbor run -p tasks/distributed-kv-k3s -e docker -a codex -m openai/gpt-5.5
```

The Docker Compose definition gives the agent's `main` container privileged access for Docker-in-Docker. This task targets Harbor's local Docker environment.

`tests/test.sh` is an intentional failing placeholder because verification is outside the current scope. No Oracle solution is included. Do not treat a run of this task as a scored evaluation until a verifier is added.

## GLM through OpenRouter

The repo-local `OpenRouterCodex` Harbor adapter configures Codex's Responses
provider and preserves the complete OpenRouter model slug. Put
`OPENROUTER_API_KEY=...` in a gitignored `.env`, then run from the repository
root (replace the env-file path with its actual location):

```sh
PYTHONPATH="$PWD" harbor run -p tasks/distributed-kv-k3s -e docker \
  --agent-import-path harbor_agents.openrouter_codex:OpenRouterCodex \
  -m z-ai/glm-5.3 --env-file /path/to/.env \
  --disable-verification --artifact /app \
  --jobs-dir .run-data/harbor-jobs --n-concurrent 1
```

The key is uploaded temporarily to the agent container, then removed when the
agent stops. `--artifact /app` collects the generated submission for the
separate deployment and load test. Verification is disabled because this task
still has a placeholder verifier. The agent container does not publish host
ports; its own `deploy.sh` is responsible for exposing ports within the
sandbox during evaluation.

The Harbor container includes the same K3s tooling and OpenRC setup as the
deployment sandbox, so the agent can exercise a fresh deployment before it
submits its files.
