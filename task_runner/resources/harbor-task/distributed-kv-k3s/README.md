# Distributed KV Harbor task

The task runner stages this Harbor task and writes one `instruction.md` from
the task prompt, versioned DigitalOcean deployment contract, and manifest
resource budget. The environment supplies only a read-only seed at
`/seed/kv.jsonl`. The deliverable is a Terraform root module, executable
`/app/deploy.sh`, and all files needed for a fresh deployment.

From the repository root, put `OPENROUTER_API_KEY=...` in the root gitignored
`.env`, then run:

```sh
uv run python -m task_runner distributed-kv-k3s generate
```

The task runner passes Harbor a filtered env file containing only the
OpenRouter key, so it does not give the generation agent the DigitalOcean
credential. It uses the repo-local `OpenRouterCodex` Harbor adapter and
collects `/app` as the generated submission. The environment provides the
Terraform, Go, Docker, and kubectl so the agent can check its submission.
Harbor's `tests/test.sh` is a placeholder;
verification is disabled until a real verifier exists.
