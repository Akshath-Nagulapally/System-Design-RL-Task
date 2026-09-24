# KV benchmark: Muse, Kimi, and Sol rerun

Run date: 2026-09-24 (America/Chicago). All three submissions were generated afresh by the same Harbor Codex/OpenRouter harness, under the 10 vCPU and 20,480 MB task budget. Submitted artifacts were not edited before deployment.

| Model | Harbor generation | Deployment job | Planned Droplet budget | Outcome |
| --- | --- | --- | --- | --- |
| Muse Spark 1.3 | `211d1e0c75f8434bac06e357a9519090` | `9ca44e54edc641759ad1217db2958dc6` | 6 vCPU, 12,288 MB | Deployment failed immediately: `deploy.sh` requires `python3`, which the deployment sandbox does not include. No load samples. |
| Kimi K3 | `df6fc27f11c0433f828a54850fbaf42a` | `5477895c1cdb4d43bd7b3861de420743` | 6 vCPU, 12,288 MB | Deployment failed: K3s bootstrap registered its etcd peer using the public IP, then restarted with a private IP, leaving K3s unable to start. No load samples. |
| GPT-6 Sol | `9a89e4b7b4b64725b1981c9b43dbc111` | `bff5b35c8d50442db30235b538b24a71` | 6 vCPU, 12,288 MB | Deployed and completed load simulation: 9,000/9,000 successful attempts. |

For Sol, the load simulator scheduled 100 requests/second for 30 seconds in each phase, with 200 maximum in flight and a five-second timeout. It sent 3,000 GETs, 3,000 PUTs, deleted 50% of the remaining Droplets (one of three, rounded down), then sent 3,000 DELETEs. SQLite recorded one crash event and no request failures or drops.

| Sol phase | Successes / attempts | Mean latency | Success-only p95 | Success-only p99 |
| --- | ---: | ---: | ---: | ---: |
| GET | 3,000 / 3,000 | 48.35 ms | 113.92 ms | 126.55 ms |
| PUT | 3,000 / 3,000 | 67.09 ms | 167.95 ms | 390.47 ms |
| Post-crash DELETE | 3,000 / 3,000 | 49.75 ms | 116.23 ms | 129.42 ms |

The earlier updated reference run (`06c393a5033744caa03cb6884687d7c9`) used five Droplets and lost two; it completed 3,000 GETs, 3,000 PUTs, and 2,683 of 3,000 post-crash DELETEs. Because Sol lost one of three machines and the reference lost two of five, these outcomes do not measure identical fault severity. The three operation types also differ, so phase latencies are not a direct before/after performance comparison.

The task runner's SQLite database is `.run-data/task-runner.sqlite3`. After every run, the DigitalOcean API showed zero task Droplets, load balancers, and projects. Muse's first earlier attempt was interrupted by local Docker storage pressure; this rerun had adequate disk space and exported a Harbor artifact.

## Patched Muse follow-up

To determine whether missing Python was the only blocker, the original Harbor artifact was copied to `.run-data/variants/muse-python3/app`. Its `deploy.sh` was changed to install `python3` with Alpine's `apk` if absent. That got the script through deployment, seed import, and readiness, but the server rejected `result.json`: two endpoint names used hyphens (`api-1`, `api-2`), while the server requires identifier-style names. The copied script was then changed to emit `api_1` and `api_2`. No other submission files were edited.

The second patched deployment, job `f9eaf4b392f64b358345c9161daab644`, succeeded. At the same 100 requests/second, 30 seconds/phase settings, SQLite recorded 3,000/3,000 successful GETs, 3,000/3,000 successful PUTs, then 1,650/3,000 successful post-crash DELETEs. The 1,350 remaining DELETE attempts comprised 684 timeouts, 663 generator drops, and three HTTP 503 responses. The server deleted one of three Droplets (`kv-1`), leaving two. Successful DELETEs averaged 985.40 ms with p95 2,246.31 ms; the mean across all recorded DELETE latencies, including timeouts, was 2,163.49 ms. GET and PUT means were 47.74 ms and 55.98 ms. Cleanup again left zero task Droplets, load balancers, and projects.

This patched result measures a corrected submission interface, not Muse's original unmodified Harbor output. It also shows that adding Python alone was insufficient for the deployment server to accept Muse's result.
