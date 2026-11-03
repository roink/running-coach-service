"""Durable idempotent jobs; ambiguous running turns require reconciliation."""

import json
import sqlite3
import time
from pathlib import Path


class Queue:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL,
                thread_id TEXT, turn_id TEXT, result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def get(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def enqueue(self, job_id, payload):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,payload,created) VALUES(?,?,?)",
                (job_id, json.dumps(payload), time.time()),
            )
            return bool(cursor.rowcount)

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in ["payload", "result"]:
            if result[field] is not None:
                result[field] = json.loads(result[field])
        return result

    def claim(self, allow_automatic=True):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT id FROM jobs WHERE state='queued' AND (? OR id NOT LIKE 'workout-%') "
                "ORDER BY created,id LIMIT 1",
                (bool(allow_automatic),),
            ).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (row[0],))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.job(row[0]) if row else None

    def attach_turn(self, job_id, thread_id, turn_id):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET thread_id=?,turn_id=? WHERE id=?", (thread_id, turn_id, job_id)
            )

    def complete(self, job_id, result):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='completed',result=?,error=NULL WHERE id=?",
                (json.dumps(result), job_id),
            )

    def hold(self, job_id, reason):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='needs_review',error=? WHERE id=?", (reason[:1000], job_id)
            )

    def mark_delivered(self, job_id):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='delivered' WHERE id=? AND state='completed'", (job_id,)
            )

    def pending_deliveries(self):
        return [
            self.job(row[0])
            for row in self.db.execute(
                "SELECT id FROM jobs WHERE state='completed' ORDER BY created,id"
            )
        ]

    def interrupted(self):
        return [
            self.job(row[0])
            for row in self.db.execute(
                "SELECT id FROM jobs WHERE state='running' ORDER BY created,id"
            )
        ]

    def close(self):
        self.db.close()
