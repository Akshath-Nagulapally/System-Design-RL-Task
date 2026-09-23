# Distributed key-value store on K3s

Implement the complete assignment in `/app/spec.md`. Work in `/app`, starting from an empty project. The spec is the source of truth for the API, correctness guarantees, deployment limits, and handoff format.

Your submission must include an executable `/app/deploy.sh`. It must create the local K3s cluster, deploy the service, import the read-only seed at `/seed/kv.jsonl`, and leave the service running after the script exits. The Harbor environment provides a privileged Docker daemon so a local K3s topology can be created inside the sandbox. You may choose the implementation language and storage design.

The runner will set `DEPLOY_OUTPUT_DIR` to a writable directory. On success, write its `result.json` exactly as described in the spec, with reachable endpoints and any required artifacts. A fresh deployment must complete within 15 minutes and remain within the stated 6 vCPU, 8 GiB RAM, and three-node limits. Include any source files, manifests, and setup steps needed for `deploy.sh` to work in a fresh copy of this environment.
