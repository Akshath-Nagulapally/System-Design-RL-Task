# One-shot deployment server

The task runner starts one server per job. `POST /deploy` accepts a gzip
submission archive once; a second upload receives HTTP 409. `GET /result`
returns 202 while deployment runs and the saved response afterward. The API
uses a bearer token when configured by the task runner.

## DigitalOcean backend

The default task-runner backend uses a DigitalOcean token from the ignored
repository root `.env`. The runner sends the archive with an
`X-Resource-Limits` JSON header derived from the task manifest. The server
creates a job Project and temporary SSH key, initializes Terraform from the
submission's `terraform/` directory, saves a plan, and reads its JSON output.
It sums every planned Droplet's vCPU and memory using DigitalOcean's size
catalog. Unknown or unmeasurable compute, non-DigitalOcean providers, and
Terraform provisioners fail validation. An over-budget plan returns HTTP 422
and `score: 0` without applying the plan. Exact-budget plans pass.

After validation the server applies that same saved plan. It then runs the
submission's `deploy.sh` inside a 4-vCPU, 4-GiB Docker sandbox with the
Terraform outputs, temporary SSH key, and read-only seed. The DigitalOcean
token is not passed to the sandbox. `deploy.sh` writes
`$DEPLOY_OUTPUT_DIR/result.json` with endpoint URLs and declared artifacts.
The server accepts only endpoint hosts matching the deployed Droplet IPs.

`POST /crash` accepts `{"count": 1}` after deployment. For each request, the
server randomly selects that many remaining Droplets from the job's Terraform
state, verifies Project membership, deletes them through the DigitalOcean API,
and records the selection. Control-plane Droplets are eligible. The request
fails if fewer Droplets remain than requested. The server waits for deletion
to be visible before returning.

`DELETE /deployment` destroys the remaining Terraform resources and then deletes the
empty Project. The task runner invokes it after load testing and on errors.
The Project organizes resources; Terraform state and the job ID provide the
teardown record.

## Local Docker backend

Set `TASK_DEPLOY_BACKEND=docker` in the task runner to run the older local
reference solution. This starts a privileged Docker-in-Docker sandbox capped
at 6 vCPU and 8 GiB, runs `deploy.sh`, and exposes declared ports through
proxy containers. It is retained for local regression tests.

Run the offline suite with:

```sh
uv run python -m unittest discover -s tests
```
