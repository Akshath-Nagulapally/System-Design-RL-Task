"""Run a steady stream of calls and retain per-call latency records."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Awaitable, Callable


Operation = Callable[[], Awaitable[Any]]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class TrafficSample:
    """One scheduled call, timestamped when it started or was dropped."""

    sequence: int
    timestamp: str
    latency_ms: float | None
    outcome: str  # success, error, timeout, or dropped
    error: str | None = None


@dataclass(frozen=True)
class TrafficResult:
    samples: tuple[TrafficSample, ...]

    @property
    def average_latency_ms(self) -> float | None:
        """Mean duration of started calls, including errors and timeouts."""

        latencies = [sample.latency_ms for sample in self.samples if sample.latency_ms is not None]
        return mean(latencies) if latencies else None

    def save_jsonl(self, path: str | Path) -> None:
        """Write one JSON record per scheduled call for plotting or analysis."""

        with Path(path).open("w", encoding="utf-8") as output:
            for sample in self.samples:
                output.write(json.dumps(asdict(sample)) + "\n")


@dataclass(frozen=True)
class LoadSimClient:
    kubeconfig: str | Path
    kubernetes_url: str
    api_url: str

    def traffic(
        self,
        operation: Operation,
        *,
        rate_per_sec: float,
        duration_s: float,
        max_in_flight: int,
        timeout_s: float,
    ) -> TrafficResult:
        """Run a probe from ordinary synchronous Python code."""

        return asyncio.run(
            self.atraffic(
                operation,
                rate_per_sec=rate_per_sec,
                duration_s=duration_s,
                max_in_flight=max_in_flight,
                timeout_s=timeout_s,
            )
        )

    async def atraffic(
        self,
        operation: Operation,
        *,
        rate_per_sec: float,
        duration_s: float,
        max_in_flight: int,
        timeout_s: float,
    ) -> TrafficResult:
        """Start calls at a fixed rate, without queueing when capacity is full."""

        if not callable(operation) or not (
            inspect.iscoroutinefunction(operation)
            or inspect.iscoroutinefunction(getattr(operation, "__call__", None))
        ):
            raise TypeError("operation must be an async callable with no arguments")
        for name, value in (
            ("rate_per_sec", rate_per_sec),
            ("duration_s", duration_s),
            ("timeout_s", timeout_s),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight <= 0:
            raise ValueError("max_in_flight must be a positive integer")

        samples: list[TrafficSample] = []
        active: set[asyncio.Task[None]] = set()
        start = time.monotonic()
        sequence = 0

        async def execute(number: int) -> None:
            timestamp = _timestamp()
            call_start = time.monotonic()
            outcome = "success"
            error: str | None = None
            try:
                await asyncio.wait_for(operation(), timeout=timeout_s)
            except TimeoutError:
                outcome, error = "timeout", "operation timed out"
            except Exception as exc:
                outcome, error = "error", f"{type(exc).__name__}: {exc}"
            latency_ms = (time.monotonic() - call_start) * 1000
            samples.append(TrafficSample(number, timestamp, latency_ms, outcome, error))

        try:
            while sequence / rate_per_sec < duration_s:
                scheduled_at = start + sequence / rate_per_sec
                await asyncio.sleep(max(0, scheduled_at - time.monotonic()))
                if len(active) >= max_in_flight:
                    samples.append(TrafficSample(sequence, _timestamp(), None, "dropped"))
                else:
                    task = asyncio.create_task(execute(sequence))
                    active.add(task)
                    task.add_done_callback(active.discard)
                sequence += 1
            if active:
                await asyncio.gather(*active)
        except asyncio.CancelledError:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise

        return TrafficResult(tuple(sorted(samples, key=lambda sample: sample.sequence)))
