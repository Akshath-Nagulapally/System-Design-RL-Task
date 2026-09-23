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
  the Terraform reference submission. The older `solution/` remains for local
  Docker regression tests.
- `deployment_server/` accepts the archive and trusted CPU/memory budget,
  creates a job Project, checks the Terraform plan, applies it, and runs the
  submission's `deploy.sh` in a token-free Docker sandbox.
- `loadsim/` records per-request results in SQLite.

## Credentials and prerequisites

Put `DIGITAL_OCEAN_API_KEY=...` and `OPENROUTER_API_KEY=...` in the ignored root
`.env` (mode 0600). The runner creates a filtered temporary Harbor env file
containing only the OpenRouter key. The deployment server alone receives the
DigitalOcean key. Install Terraform, Docker, and Python 3.11+; give Docker at
least 4 GiB for the cloud deployment script sandbox. The DigitalOcean token
needs read/create/delete permissions for Projects, Droplets, VPCs, SSH keys,
and firewalls, plus project resource assignment and size lookup.

## Commands

```sh
uv run python -m task_runner distributed-kv-k3s generate
uv run python -m task_runner distributed-kv-k3s deploy /path/to/submission
uv run python -m task_runner distributed-kv-k3s loadsim --job-id JOB_ID
uv run python -m task_runner distributed-kv-k3s cleanup JOB_ID
```

Omit the submission path to deploy the Terraform reference solution. `deploy`
leaves the cloud service running; `loadsim` runs GET and PUT traffic, crashes
one random job Droplet, runs DELETE traffic, and cleans up
afterward. `cleanup` is available if the run is interrupted. Project deletion
follows Terraform destroy. Job state and logs live under ignored `.run-data/`.
A rejected over-budget plan receives HTTP 422 with a zero score indicator and
is never applied.

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
