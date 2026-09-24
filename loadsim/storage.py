"""SQLite persistence shared by task-specific load simulations."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

from .traffic import TrafficResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunRecorder:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.connection = sqlite3.connect(path, timeout=30)
        path.chmod(0o600)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                submission_path TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                api_url TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS request_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                phase TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                latency_ms REAL,
                outcome TEXT NOT NULL,
                error TEXT,
                UNIQUE(job_id, phase, sequence)
            );
            CREATE INDEX IF NOT EXISTS request_samples_job_id ON request_samples(job_id);
            CREATE TABLE IF NOT EXISTS fault_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                happened_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def create_job(self, job_id: str, task_name: str, submission_path: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO jobs (id, task_name, submission_path, status, started_at) "
                "VALUES (?, ?, ?, 'deploying', ?)",
                (job_id, task_name, submission_path, utc_now()),
            )

    def update_job(self, job_id: str, status: str, *, api_url: str | None = None,
                   error: str | None = None) -> None:
        finished = utc_now() if status in ("completed", "failed", "cleaned") else None
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status = ?, finished_at = COALESCE(?, finished_at), "
                "api_url = COALESCE(?, api_url), error = COALESCE(?, error) WHERE id = ?",
                (status, finished, api_url, error, job_id),
            )

    def save_phase(self, job_id: str, phase: str, result: TrafficResult) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO request_samples "
                "(job_id, phase, sequence, timestamp, latency_ms, outcome, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((job_id, phase, sample.sequence, sample.timestamp, sample.latency_ms,
                  sample.outcome, sample.error) for sample in result.samples),
            )

    def save_fault(self, job_id: str, kind: str, details: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO fault_events (job_id, happened_at, kind, details_json) VALUES (?, ?, ?, ?)",
                (job_id, utc_now(), kind, json.dumps(details)),
            )

    def summary(self, job_id: str) -> dict:
        count, successes, failures, dropped, average = self.connection.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome IN ('error', 'timeout') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome = 'dropped' THEN 1 ELSE 0 END), AVG(latency_ms) "
            "FROM request_samples WHERE job_id = ?", (job_id,),
        ).fetchone()
        phases = {}
        for phase, phase_requests, phase_successes, phase_failures, phase_dropped, latency in self.connection.execute(
            "SELECT phase, COUNT(*), "
            "SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome IN ('error', 'timeout') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome = 'dropped' THEN 1 ELSE 0 END), AVG(latency_ms) "
            "FROM request_samples WHERE job_id = ? GROUP BY phase", (job_id,),
        ):
            phases[phase] = {"requests": phase_requests, "successes": phase_successes or 0,
                             "failures": phase_failures or 0, "dropped": phase_dropped or 0,
                             "average_latency_ms": latency}
        faults = [{"kind": kind, "happened_at": happened_at, "details": json.loads(details)}
                  for kind, happened_at, details in self.connection.execute(
                      "SELECT kind, happened_at, details_json FROM fault_events WHERE job_id = ? ORDER BY id",
                      (job_id,))]
        return {"job_id": job_id, "requests": count, "successes": successes or 0,
                "failures": failures or 0, "dropped": dropped or 0,
                "average_latency_ms": average, "phases": phases, "faults": faults}
