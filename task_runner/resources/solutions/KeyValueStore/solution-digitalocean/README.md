# DigitalOcean K3s reference submission

`terraform/` uses the server-supplied private network and creates a one-server/two-worker K3s cluster on
three DigitalOcean Droplets, a job SSH key, a firewall, and Project membership.
Each Droplet uses a two-vCPU, 2048-MB size. The complete cluster therefore
plans six vCPUs and 6144 MB of Droplet memory, under the task's six-vCPU and
8192-MB budget. Terraform exposes `server_ip` and `droplet_ips` outputs.

The deployment server validates and applies the Terraform plan. It then runs
`deploy.sh` in a token-free Docker sandbox with the outputs, a temporary SSH
key, and the seed file mounted read-only. The script builds the Go API for
Linux AMD64, imports its image into all K3s nodes, deploys the API and a
three-member etcd cluster, imports the seed, checks readiness, and writes
`result.json` and a kubeconfig. The runner keeps the deployment active for
load and fault probes, then destroys the Terraform state and Project.

Use the task runner to deploy; this directory does not contain DigitalOcean
credentials. Set `DIGITAL_OCEAN_API_KEY` in the repository root's ignored
`.env`. No API key is included in the submission archive.
