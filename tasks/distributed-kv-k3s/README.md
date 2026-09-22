# Distributed KV Harbor task

This Harbor task asks a coding agent to implement the assignment copied from `solutions/KeyValueStore/spec.md`. The agent sees `/app/spec.md` and a small read-only seed at `/seed/kv.jsonl`. Its deliverable is an executable `/app/deploy.sh` plus everything required for a fresh deployment.

The intended harness is Harbor's Codex CLI agent. From the repository root, a future run can use:

```sh
harbor run -p tasks/distributed-kv-k3s -e docker -a codex -m openai/gpt-5.5
```

The Docker Compose definition gives the agent's `main` container privileged access for Docker-in-Docker and publishes the API and Kubernetes ports. This task therefore targets Harbor's local Docker environment.

`tests/test.sh` is an intentional failing placeholder because verification is outside the current scope. No Oracle solution is included. Do not treat a run of this task as a scored evaluation until a verifier is added.
