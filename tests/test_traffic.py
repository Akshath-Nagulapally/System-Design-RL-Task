import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from loadsim import LoadSimClient


CLIENT = LoadSimClient("kubeconfig", "https://kubernetes.example", "https://api.example")


class TrafficTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_rate_records_calls_and_exports_them(self):
        async def operation():
            await asyncio.sleep(0.002)

        result = await CLIENT.atraffic(
            operation, rate_per_sec=20, duration_s=0.11, max_in_flight=2, timeout_s=1
        )

        self.assertEqual([sample.sequence for sample in result.samples], [0, 1, 2])
        self.assertTrue(all(sample.outcome == "success" for sample in result.samples))
        self.assertGreater(result.average_latency_ms, 0)
        self.assertTrue(all(datetime.fromisoformat(sample.timestamp).tzinfo for sample in result.samples))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            result.save_jsonl(path)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["sequence"], 0)
            self.assertIn("latency_ms", rows[0])

    async def test_concurrency_cap_drops_instead_of_queueing(self):
        running = 0
        peak = 0

        async def slow_operation():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.08)
            finally:
                running -= 1

        result = await CLIENT.atraffic(
            slow_operation, rate_per_sec=100, duration_s=0.07, max_in_flight=2, timeout_s=1
        )

        self.assertLessEqual(peak, 2)
        self.assertEqual(len(result.samples), 7)
        self.assertTrue(any(sample.outcome == "dropped" for sample in result.samples))
        self.assertTrue(all(sample.latency_ms is None for sample in result.samples if sample.outcome == "dropped"))

    async def test_timeout_and_error_are_visible(self):
        async def slow_operation():
            await asyncio.sleep(0.05)

        timeout = await CLIENT.atraffic(
            slow_operation, rate_per_sec=1, duration_s=0.01, max_in_flight=1, timeout_s=0.01
        )
        self.assertEqual(timeout.samples[0].outcome, "timeout")
        self.assertGreater(timeout.samples[0].latency_ms, 0)

        async def broken_operation():
            raise RuntimeError("failed")

        error = await CLIENT.atraffic(
            broken_operation, rate_per_sec=1, duration_s=0.01, max_in_flight=1, timeout_s=1
        )
        self.assertEqual(error.samples[0].outcome, "error")
        self.assertIn("RuntimeError", error.samples[0].error)


if __name__ == "__main__":
    unittest.main()
