# DigitalOcean deployment contract

Submit an executable `/app/deploy.sh`, a `/app/terraform` root module, and
all application source and Kubernetes manifests needed for a fresh deployment.
Use the `digitalocean/digitalocean` Terraform provider. Terraform must create
all DigitalOcean compute resources as Droplets. You may choose the number and
sizes of Droplets within the task's total CPU and memory budget. Do not use
managed Kubernetes, autoscale pools, provisioners, or other commands to create
cloud resources outside the visible Terraform plan.

The deployment server runs Terraform init, plan, budget validation, and apply
before it runs `deploy.sh`. It supplies `deployment_project_id`,
`deployment_region`, `deployment_vpc_id`, `deployment_vpc_cidr`,
`deployment_ssh_public_key`, and `deployment_k3s_token`
as Terraform variables. Terraform must output `server_ip` and `droplet_ips`.
Do not put an API token in the repository. The server owns the DigitalOcean
credential and associates the resulting resources with its per-job Project.
Use the supplied shared VPC; do not create a VPC in the job module.

Use K3s on the provisioned Droplets. After Terraform apply, `deploy.sh` gets
`DEPLOY_TERRAFORM_OUTPUTS` (path to Terraform's JSON outputs),
`DEPLOY_SSH_PRIVATE_KEY` (path to the matching private key),
`DEPLOY_OUTPUT_DIR`, and a read-only seed at `KV_SEED_FILE`. It must build
and distribute the API image, deploy Kubernetes resources, import the seed,
verify readiness, and write the normal `result.json` and kubeconfig artifact.
The script must not create DigitalOcean resources.
