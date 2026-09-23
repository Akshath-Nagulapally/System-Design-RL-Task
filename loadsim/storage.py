"""SQLite persistence shared by task-specific load simulations."""

from __future__ import annotations

import sqlite3
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

    def summary(self, job_id: str) -> dict:
        count, successes, failures, dropped, average = self.connection.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome IN ('error', 'timeout') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome = 'dropped' THEN 1 ELSE 0 END), AVG(latency_ms) "
            "FROM request_samples WHERE job_id = ?", (job_id,),
        ).fetchone()
        return {"job_id": job_id, "requests": count, "successes": successes or 0,
                "failures": failures or 0, "dropped": dropped or 0,
                "average_latency_ms": average}
