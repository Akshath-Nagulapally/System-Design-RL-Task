"""KV task traffic and correctness checks, run by task-runner loadsim."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path

import httpx

from loadsim import LoadSimClient, RunRecorder


def first_seed_record(path: Path) -> tuple[str, str]:
    with path.open(encoding="utf-8") as seed:
        for line in seed:
            if line.strip():
                record = json.loads(line)
                return record["key"], record["value"]
    raise ValueError(f"seed file is empty: {path}")


async def run_traffic(client: LoadSimClient, recorder: RunRecorder, job_id: str,
                      seed_record: tuple[str, str], *, rate: float, duration: float,
                      max_in_flight: int, timeout: float) -> None:
    seed_key, seed_value = seed_record
    async with httpx.AsyncClient(base_url=client.api_url, timeout=timeout) as http:
        health = await http.get("/healthz")
        health.raise_for_status()
        seeded = await http.get(f"/v1/kv/{seed_key}")
        seeded.raise_for_status()
        if seeded.json().get("value") != seed_value:
            raise RuntimeError("deployed service did not import the supplied seed")

        probe_key = f"e2e-{job_id[:12]}-probe"
        probe = await http.put(f"/v1/kv/{probe_key}", json={"value": "probe"})
        probe.raise_for_status()
        if not probe.json().get("version"):
            raise RuntimeError("PUT did not return a version")
        read_back = await http.get(f"/v1/kv/{probe_key}")
        read_back.raise_for_status()
        if read_back.json().get("value") != "probe":
            raise RuntimeError("GET did not return the written value")
        deleted = await http.delete(f"/v1/kv/{probe_key}")
        if deleted.status_code != 204:
            raise RuntimeError(f"DELETE returned {deleted.status_code}, expected 204")
        missing = await http.get(f"/v1/kv/{probe_key}")
        if missing.status_code != 404:
            raise RuntimeError(f"deleted key returned {missing.status_code}, expected 404")

        async def read_seed() -> None:
            response = await http.get(f"/v1/kv/{seed_key}")
            response.raise_for_status()
            if response.json().get("value") != seed_value:
                raise RuntimeError("seed value changed during read traffic")

        keys: list[str] = []
        numbers = itertools.count()

        async def write_unique() -> None:
            key = f"e2e-{job_id[:12]}-{next(numbers)}"
            response = await http.put(f"/v1/kv/{key}", json={"value": "traffic"})
            response.raise_for_status()
            if not response.json().get("version"):
                raise RuntimeError("PUT did not return a version")
            keys.append(key)

        async def delete_written() -> None:
            key = next(delete_keys)
            response = await http.delete(f"/v1/kv/{key}")
            if response.status_code != 204:
                raise RuntimeError(f"DELETE returned {response.status_code}, expected 204")

        settings = dict(rate_per_sec=rate, duration_s=duration,
                        max_in_flight=max_in_flight, timeout_s=timeout)
        recorder.save_phase(job_id, "get", await client.atraffic(read_seed, **settings))
        recorder.save_phase(job_id, "put", await client.atraffic(write_unique, **settings))
        if not keys:
            raise RuntimeError("no PUT request succeeded; cannot run DELETE traffic")
        delete_keys = itertools.cycle(keys)
        recorder.save_phase(job_id, "delete", await client.atraffic(delete_written, **settings))


def main() -> None:
    result_path = Path(os.environ["TASK_DEPLOYMENT_JSON"])
    job_id = os.environ["TASK_JOB_ID"]
    recorder = RunRecorder(os.environ["TASK_DB_PATH"])
    try:
        client = LoadSimClient.from_deployment_result(json.loads(result_path.read_text()),
                                                      result_path.parent)
        seed_record = first_seed_record(Path(os.environ["TASK_SEED_PATH"]))
        asyncio.run(run_traffic(client, recorder, job_id, seed_record,
                                rate=float(os.environ["TASK_RATE"]),
                                duration=float(os.environ["TASK_DURATION"]),
                                max_in_flight=int(os.environ["TASK_MAX_IN_FLIGHT"]),
                                timeout=float(os.environ["TASK_TIMEOUT"])))
    finally:
        recorder.close()


if __name__ == "__main__":
    main()
