# KV store: GLM 5.3 versus reference

## Setup

On 2026-09-22 (America/Chicago), Harbor generated
[`glm53-generated`](../tasks/solutions/KeyValueStore/glm53-generated) with
`z-ai/glm-5.3` through the OpenRouter Codex adapter. The completed Harbor
trial is `kv-glm53-stablekey-20260923/distributed-kv-k3s__tHKcUcy` in the
gitignored `.run-data/harbor-jobs` directory. Harbor verification was disabled;
the task's verifier is a placeholder. The external deployment and load runner
produced the results below. The generated source was copied verbatim from the
Harbor artifact, with no implementation edits.

Each system was deployed from a fresh submission through
`scripts/run_kv_iteration.py` in a privileged Docker sandbox capped at 6 vCPU
and 8 GiB. Docker Desktop reported 7.65 GiB of available memory. The runner
checked readiness, seed import, a PUT/GET/DELETE round trip, then scheduled
GET of one seed key, PUT of unique keys, and DELETE of those keys. Each phase
lasted 10 seconds. Requests had a 5-second timeout. The 100 request/s test
allowed 100 concurrent requests; the 300 request/s test allowed 150. Full
per-request samples are in the gitignored `.run-data/kv-runs.sqlite3` database.

## Results

| Offered rate, each phase | System | Succeeded / scheduled | Dropped | Request errors | Mean successful latency | p95 successful latency | p99 successful latency |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 100/s | GLM 5.3 | 3,000 / 3,000 | 0 | 0 | 8.17 ms | 45.87 ms | 47.94 ms |
| 100/s | Reference | 3,000 / 3,000 | 0 | 0 | 8.71 ms | 45.93 ms | 47.74 ms |
| 300/s | GLM 5.3 | 8,535 / 9,000 | 465 | 0 | 172.32 ms | 1,176.47 ms | 2,319.39 ms |
| 300/s | Reference | 7,810 / 9,000 | 1,096 | 94 | 251.39 ms | 1,162.00 ms | 2,551.12 ms |

The 94 reference errors at 300/s were `RemoteProtocolError: Server disconnected
without sending a response.` Dropped calls never started because the load
generator's concurrency limit was full; they are excluded from latency
percentiles. The reference's mean over *all started calls*, including those
errors, was 269.65 ms.

| Offered rate | System | Phase | Succeeded / scheduled | Mean success | p95 success |
| --- | --- | --- | ---: | ---: | ---: |
| 100/s | GLM 5.3 | GET | 1,000 / 1,000 | 4.88 ms | 7.27 ms |
| 100/s | Reference | GET | 1,000 / 1,000 | 5.42 ms | 8.82 ms |
| 100/s | GLM 5.3 | PUT | 1,000 / 1,000 | 14.45 ms | 47.19 ms |
| 100/s | Reference | PUT | 1,000 / 1,000 | 15.44 ms | 47.29 ms |
| 100/s | GLM 5.3 | DELETE | 1,000 / 1,000 | 5.17 ms | 7.32 ms |
| 100/s | Reference | DELETE | 1,000 / 1,000 | 5.27 ms | 7.26 ms |
| 300/s | GLM 5.3 | GET | 2,535 / 3,000 | 545.18 ms | 2,213.84 ms |
| 300/s | Reference | GET | 3,000 / 3,000 | 110.95 ms | 313.21 ms |
| 300/s | GLM 5.3 | PUT | 3,000 / 3,000 | 24.49 ms | 48.67 ms |
| 300/s | Reference | PUT | 1,810 / 3,000 | 708.07 ms | 2,464.39 ms |
| 300/s | GLM 5.3 | DELETE | 3,000 / 3,000 | 5.07 ms | 10.18 ms |
| 300/s | Reference | DELETE | 3,000 / 3,000 | 116.30 ms | 311.21 ms |

At 300/s, GLM 5.3 accepted more of the offered traffic overall (94.8% versus
86.8%). Its advantage came from PUT and DELETE. The reference handled the hot
key GET phase better: GLM 5.3 dropped 465 reads while the reference dropped
none. This is a phase-specific result from one short run, not a stable capacity
estimate.

## Design comparison

| Area | GLM 5.3 | Reference |
| --- | --- | --- |
| Cluster | Three K3s nodes, created by k3d | Three K3s nodes, created by k3d |
| Storage | Three-member etcd StatefulSet, PVCs, required node anti-affinity | Three-member etcd StatefulSet, PVCs, required node anti-affinity |
| API | Three Go replicas; direct NodePort | Two Go replicas pinned to workers; Traefik ingress |
| Read/write path | Default linearizable etcd reads; Raft-backed writes; conditional PUT uses an etcd transaction | Same consistency model and transaction-based conditional PUT |
| Seed | Temporary Kubernetes importer pod | Host-side importer via an etcd port-forward |
| Readiness | `/healthz` returns 200 once the HTTP process is running, even if etcd later loses quorum | `/healthz` performs a linearizable etcd read and returns 503 when quorum is unavailable |
| Verification in source | No automated tests in the generated artifact; the model ran `go test ./...` and a clean deployment | HTTP and store tests, including optional live etcd integration tests |

Both implementations deployed and passed the runner's basic data checks. The
GLM 5.3 artifact uses the three API replicas its manifest declares, unlike the
earlier GLM 5 attempt. Its direct NodePort route appears relevant to its
stronger write/delete performance, but the current tests do not isolate the
network route from differences in code, etcd version, or deployment settings.

## Limits and next checks

These are one-run measurements on one Docker Desktop host. The load generator,
proxy, Docker-in-Docker, and Kubernetes cluster all share that host; this is
not a production throughput benchmark. We did not exercise concurrent
conditional writes, worker loss, partition/recovery, or durability after a
node failure. The model's readiness behavior under quorum loss is a concrete
spec gap from source review; failure-injection testing should confirm its
impact. Repeat each rate several times before treating small latency
differences as a ranking.

Job IDs in SQLite: GLM 100/s `632f6eeacd3b491ca9cdef9646a0913d`, reference
100/s `0af3215435704e7da86ed79ab1139ff7`, GLM 300/s
`c2b9e14367b541d0bc97db770a7178f4`, reference 300/s
`f9d69b2e76754041a13bfbfc24f3dc71`.
