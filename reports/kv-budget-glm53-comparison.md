# KV benchmark: updated reference vs. GLM 5.3

Run date: 2026-09-23 (America/Chicago). Source: the task runner's local
`.run-data/task-runner.sqlite3` database and the saved Harbor submission.

## Test configuration

- Task budget: 10 vCPUs and 20,480 MB of Droplet memory.
- Both Terraform plans requested five `s-2vcpu-4gb` Droplets, exactly the
  budget. The reference used one DigitalOcean load balancer; GLM requested two.
- Successful deployments were tested at 100 scheduled requests/second for
  30 seconds per phase, with at most 200 in flight and a five-second request
  timeout. The task script sends GET, then PUT, deletes 50% of the remaining
  Droplets (two of five), then sends DELETE.
- GET reads one seed key; PUT creates distinct keys; DELETE removes keys from
  the PUT phase. These are different operation types, so phase latencies are
  not a like-for-like before/after comparison.

## Final results from SQLite

| Submission | Deployment | GET | PUT | Post-crash DELETE | Fault events |
| --- | --- | ---: | ---: | ---: | ---: |
| Updated reference | Succeeded | 3,000/3,000 | 3,000/3,000 | 2,683/3,000 | 1; two Droplets removed |
| GLM 5.3 Harbor artifact | Failed during `deploy.sh` | Not run | Not run | Not run | 0 |

The reference's post-crash phase had 304 HTTP 503 errors and 13 generator
drops. Its successful request rate was 89.4% of scheduled attempts, or 89.8%
of requests actually sent. Success-only p95 latency was 115.9 ms for GET,
119.6 ms for PUT, and 1,338.8 ms for post-crash DELETE. The corresponding
p99 latencies were 127.8 ms, 136.6 ms, and 2,343.9 ms.

GLM's Terraform plan passed the resource policy, but its generated deployment
script exhausted its SSH wait. The script constructed an SSH command whose
destination was the literal host `root` and then passed each Droplet IP as a
separate argument. The five Droplets were independently reachable and had
completed cloud-init. The failed job has zero request samples and zero fault
events in SQLite. A separate static review found that the script also refers
to `DEPLOY_K3S_TOKEN`, which the deployment contract does not supply; that
later step was never executed in this run. The GLM artifact was not edited.

## Reference tuning runs

The same traffic settings were used in each iteration. Every iteration
completed 3,000 GETs and 3,000 PUTs before the crash.

| Reference iteration | Post-crash DELETE successes | Errors | Timeouts | Drops |
| --- | ---: | ---: | ---: | ---: |
| Five-server cluster, ingress routing | 456 | 815 | 574 | 1,155 |
| Direct API routing and five CoreDNS replicas | 1,748 | 1,169 | 0 | 83 |
| Reachable etcd endpoints and dynamic load-balancer backends | 2,683 | 304 | 0 | 13 |

This is one random crash per iteration, not a distribution of outcomes across
all possible two-node combinations. The final random selection removed two
peer servers; it did not remove the bootstrap server. The results establish
that the final reference deployed and handled this specific fault, with
transient 503s during failover. They do not prove that every two-node failure
has the same outcome.

## Reproduce the stored counts

The local SQLite database is ignored by Git. The final reference job is
`06c393a5033744caa03cb6884687d7c9`; the GLM job is
`e3e6f6e97aa348639ce82f6e0813e84a`.

```sql
SELECT phase, outcome, COUNT(*)
FROM request_samples
WHERE job_id = '06c393a5033744caa03cb6884687d7c9'
GROUP BY phase, outcome
ORDER BY phase, outcome;
```

After each run, DigitalOcean was checked for remaining task resources. The
final check found zero Droplets, zero load balancers, and zero task Projects.
