# Distributed Key-Value Service

This project deploys a three-node K3s cluster on DigitalOcean Droplets, an
external three-member etcd cluster for application data, and a replicated Go
HTTP API. The deployment uses three `s-2vcpu-2gb` Droplets, matching the
maximum allowed six vCPUs while staying within 8192 MB of Droplet memory.

## Deploy

The evaluation runner applies `terraform/` first, then invokes:

```sh
./deploy.sh
```

The script builds and distributes the API image, creates the external etcd
cluster, imports the read-only seed through the Go importer, verifies
readiness, and writes `result.json` plus a `kubeconfig` artifact.

## API

- `PUT /v1/kv/{key}` with `{"value":"..."}`; supports quoted `If-Match`.
- `GET /v1/kv/{key}`; returns value, version, and `ETag`.
- `DELETE /v1/kv/{key}`; returns HTTP 204.
- `GET /healthz`; returns HTTP 200 only when etcd is reachable.

Keys contain 1–128 URL-safe characters (`A-Z`, `a-z`, `0-9`, `-`, `_`), and
values are UTF-8 strings of at most 4096 bytes.
