# DigitalOcean K3s reference submission

`terraform/` uses the server-supplied private network and creates five K3s
servers with embedded etcd, a job SSH key, a firewall, a load balancer, and
Project membership. A job tag selects load balancer backends dynamically. Each
Droplet uses two vCPUs and 4096 MB. The cluster plans
10 vCPUs and 20,480 MB of Droplet memory, exactly the task budget. Terraform
exposes the bootstrap server, Droplet, and load balancer IPs. The load balancer
checks the API on each VM directly, so traffic no longer depends on ingress
pods surviving the VM crash.

The deployment server validates and applies the Terraform plan. It then runs
`deploy.sh` in a token-free Docker sandbox with the outputs, a temporary SSH
key, and the seed file mounted read-only. The script builds the Go API for
Linux AMD64, imports its image into all K3s nodes, deploys the API and a
five-member application etcd cluster and five host-network API replicas, spreads
CoreDNS across the five servers, imports the seed,
checks readiness, and writes `result.json` and a kubeconfig. Both exposed
endpoints use the load balancer so either remains reachable after the
benchmark deletes two of the five Droplets. The runner keeps the deployment
active for load and fault probes, then destroys the Terraform state and Project.

Use the task runner to deploy; this directory does not contain DigitalOcean
credentials. Set `DIGITAL_OCEAN_API_KEY` in the repository root's ignored
`.env`. No API key is included in the submission archive.
