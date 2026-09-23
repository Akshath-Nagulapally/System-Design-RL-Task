# Distributed KV Harbor task

The task runner stages this Harbor task and copies
`task_runner/tasks/distributed-kv-k3s/prompt.md` to Harbor's `instruction.md` before a
generation. The agent sees `/app/spec.md` and a read-only seed at
`/seed/kv.jsonl`. Its deliverable is an executable `/app/deploy.sh` and all
files needed for a fresh deployment.

From the repository root, put `OPENROUTER_API_KEY=...` in this directory's
gitignored `.env`, then run:

```sh
uv run python -m task_runner distributed-kv-k3s generate
```

The task runner uses the repo-local `OpenRouterCodex` Harbor adapter and
collects `/app` as the generated submission. The environment provides the
same K3s tooling and OpenRC setup as the deployment sandbox so the agent can
test its deployment script. Harbor's `tests/test.sh` is a placeholder;
verification is disabled until a real verifier exists.
