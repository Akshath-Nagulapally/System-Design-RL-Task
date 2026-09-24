# Distributed KV service

The deployment files are the GPT-6 Sol Harbor submission generated in run
`9a89e4b7b4b64725b1981c9b43dbc111`. Its DigitalOcean deployment job
`bff5b35c8d50442db30235b538b24a71` completed 3,000 GETs, 3,000 PUTs,
and 3,000 post-crash DELETEs after one of three Droplets was deleted. See
`reports/kv-three-model-rerun-2026-09-24.md` for the measured latencies.
This validates that run and fault selection; it is not a claim about every
possible one-node failure.

This deployment uses three DigitalOcean `s-2vcpu-4gb` Droplets (6 vCPUs,
12 GiB), three K3s servers with their own embedded control-plane datastore,
and a **separate** three-member etcd v3.6.14 application cluster. Each
application-etcd member has a node-local persistent volume, with strict
pod anti-affinity to keep members on separate machines. An API pod runs on
every machine, and a DigitalOcean load balancer forwards healthy API traffic
and Kubernetes API traffic. API health is based on a linearizable etcd read.

`terraform/` is the root module. The deployment server supplies its six
`deployment_*` variables, runs Terraform, and passes the Terraform JSON
outputs plus SSH credentials to `deploy.sh`. The script installs K3s
v1.35.5+k3s1, builds the Go 1.25.1 image, distributes it to every node,
deploys `k8s/kv.yaml`, streams `KV_SEED_FILE` to the Go seed importer,
and checks the public load balancer before writing
`$DEPLOY_OUTPUT_DIR/result.json` and `kubeconfig`. Only Terraform creates
DigitalOcean resources. No API credential is kept in this repository.

For a manual fresh deployment, run `terraform init`, `terraform plan`, and
`terraform apply` in `terraform/` with the supplied deployment variables, then
create the JSON outputs with `terraform output -json`. Set
`DEPLOY_TERRAFORM_OUTPUTS`, `DEPLOY_SSH_PRIVATE_KEY`, `DEPLOY_OUTPUT_DIR`, and
`KV_SEED_FILE`, and run `/app/deploy.sh` from a machine with Docker, kubectl,
SSH, jq, and curl. The API is exposed over HTTP at the `api` endpoint in
`result.json`; the supplied kubeconfig is an administrative credential and
must be handled privately. Node-local data survives a single machine loss
because etcd requires two replicated members to acknowledge writes.
