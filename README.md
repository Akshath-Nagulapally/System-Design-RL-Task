# System design task runner

The task runner generates a submission with Harbor, deploys it on DigitalOcean,
runs the task's traffic probe and crash fault, and destroys the job's cloud resources. The
current task is a linearizable KV service on K3s.

## Layout

- `task_runner/tasks/distributed-kv-k3s/` contains the task manifest, KV
  requirements, and load probe.
- `task_runner/contracts/digitalocean-k3s-v1.md` is the reusable deployment
  contract appended to the agent instruction.
- `task_runner/resources/solutions/KeyValueStore/solution-digitalocean/` is
  the GPT-6 Sol submission that completed the live 9,000-request run. It uses
  6 vCPUs and 12 GiB, within the current 10-vCPU/20-GiB budget. The older
  `solution/` remains for local Docker regression tests.
- `deployment_server/` accepts the archive and trusted CPU/memory budget,
  creates a job Project, checks the Terraform plan, applies it, and runs the
  submission's `deploy.sh` in a token-free Docker sandbox.
- `loadsim/` records per-request results in SQLite.

## Credentials and prerequisites

Put `DIGITAL_OCEAN_API_KEY=...` and `OPENROUTER_API_KEY=...` in the ignored root
`.env` (mode 0600). The runner creates a filtered temporary Harbor env file
containing only the OpenRouter key. The deployment server alone receives the
DigitalOcean key. Install `uv`, Terraform, Docker, and Python 3.11+; give Docker at
least 4 GiB for the cloud deployment script sandbox. The DigitalOcean token
needs read/create/delete permissions for Projects, Droplets, VPCs, SSH keys,
and firewalls, plus project resource assignment and size lookup.

## Generate, deploy, and test a submission

Run these commands from the repository root:

1. Generate a submission with Harbor. The task manifest selects `z-ai/glm-5.3`
   by default. Change `generation.model` in
   `task_runner/tasks/distributed-kv-k3s/task_manifest.json` to set the model
   for subsequent runs, or use `--model MODEL_ID` for one run. Copy the
   `submission_path` printed by this command.

   ```sh
   uv run python -m task_runner distributed-kv-k3s generate
   ```

   For example, select GPT-6 Sol for one generation:

   ```sh
   uv run python -m task_runner distributed-kv-k3s generate --model openai/gpt-6-sol
   ```

2. Deploy that generated submission. Replace the example path with the exact
   `submission_path` from step 1. The command prints a `job_id` when deployment
   succeeds.

   ```sh
   uv run python -m task_runner distributed-kv-k3s deploy "/absolute/path/from/submission_path"
   ```

3. Run the task's load simulator against the deployment. With exactly one active
   deployment for this task, the job ID is optional. Pass `--job-id JOB_ID` to
   select a particular deployment when several are active.

   ```sh
   uv run python -m task_runner distributed-kv-k3s loadsim --job-id JOB_ID \
     --rate 100 --duration 30 --max-in-flight 200 --timeout 5
   ```

The loadsim command does **not** take a submission path or a loadsim file path.
`task_runner/tasks/distributed-kv-k3s/task_manifest.json` selects
`./loadsim.py` in that same task directory. That script checks the deployed KV
API, sends GET traffic, then PUT traffic, requests deletion of 50% of the job's
remaining Droplets (rounded down, minimum one), and finally sends DELETE
traffic. The command above matches the stress settings used in `reports/`:
100 scheduled requests per second for 30 seconds **per phase**, with at most
200 in flight and a five-second timeout. Omit those flags for the short
functional defaults of 10 requests per second for five seconds per phase.
Results are recorded in `.run-data/task-runner.sqlite3` in the `jobs`,
`request_samples`, and `fault_events` tables.

`deploy` leaves cloud resources running until `loadsim` finishes. `loadsim`
cleans them up even if the traffic script fails. If a run is interrupted before
cleanup, run:

```sh
uv run python -m task_runner distributed-kv-k3s cleanup JOB_ID
```

To deploy the Terraform reference solution instead of a generated submission,
omit the submission path from `deploy`. Project deletion follows Terraform
destroy. Job state and logs live under ignored `.run-data/`. A rejected
over-budget plan receives HTTP 422 with a zero score indicator and is never
applied.

Set `TASK_DEPLOY_REGION` to choose another DigitalOcean region. Set
`TASK_RUNNER_STATE_DIR` for another local state directory. For the older local
Docker reference test, set `TASK_DEPLOY_BACKEND=docker` and pass the old
`solution/` directory explicitly.

## Verification

```sh
uv run python -m unittest discover -s tests
terraform -chdir=task_runner/resources/solutions/KeyValueStore/solution-digitalocean/terraform validate
```

The unit tests and Terraform syntax check do not create cloud resources. A
full deployment requires a live DigitalOcean account and incurs resource
charges until cleanup completes.

## Add a task

Clone an existing task's manifest, prompt, and load script:

```sh
uv run python -m task_runner new-task my-task --from distributed-kv-k3s
```

Edit `task_runner/tasks/my-task/task_manifest.json` to select the reference
submission, Harbor task environment, seed, deployment contract, resource
limits, and default generation model. Then replace the copied `prompt.md` and
`loadsim.py` with the new task's requirements and traffic checks. Shared
resources can be reused; copy and change the Harbor environment when the
task needs different tools or seed data. The current deployment backend
supports the DigitalOcean K3s contract, so another deployment platform also
needs a backend and contract implementation. See
`task_runner/tasks/README.md` for the manifest fields and setup checklist.
