# Adding a task

Run `uv run python -m task_runner new-task NAME --from distributed-kv-k3s` from
the repository root. The command copies only the task-owned files into
`task_runner/tasks/NAME/`; it keeps links to the source task's shared
resources. A cloned task is a starting point, not a new benchmark until its
requirements and load script are replaced.

Edit `task_manifest.json`:

| Field | Purpose |
| --- | --- |
| `solution_repository` | Reference submission used when `deploy` has no path. |
| `prompt` | Base instructions for the agent. |
| `loadsim_script` | Python script executed by `loadsim`. |
| `harbor_task` | Harbor environment and tools available during generation. |
| `seed` | Read-only seed file mounted for deployment and load checks. |
| `deployment_contract` | Contract appended to the prompt; currently `digitalocean-k3s-v1`. |
| `resource_limits` | Trusted CPU and memory budget shown in the prompt and enforced by deployment. |
| `generation.model` | Default OpenRouter model ID. `generate --model` overrides it for one run. |
| `generation.agent_import_path` | Harbor adapter used for generation. |

All paths in the manifest are relative to its task directory and must resolve
inside the repository. For a different benchmark, replace the copied
`prompt.md`, `loadsim.py`, and seed. A different software stack may also need
a separate Harbor environment. A different cloud or deployment contract needs
matching deployment-server support; changing the manifest alone does not add
that support.

Check the task with `Task.load(NAME)` or run `generate`, then deploy a known
reference submission and run `loadsim` before benchmarking model output.
