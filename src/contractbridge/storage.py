from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .util import canonical, digest


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Revisioned aggregates + append-only audit. PostgreSQL in Compose; SQLite for local use."""

    def __init__(self, url: str):
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        self.url = url
        self.lock = threading.RLock()
        if not self.postgres:
            self.path = Path(url.removeprefix("sqlite:///"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            self.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, revision BIGINT NOT NULL, document TEXT NOT NULL)",
            )
            self.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS migration_revisions (migration_id TEXT NOT NULL, revision BIGINT NOT NULL, document TEXT NOT NULL, PRIMARY KEY (migration_id, revision))",
            )
            self.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL)",
            )
            self.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, document TEXT NOT NULL)",
            )
            conn.commit()

    @contextmanager
    def connection(self):
        if self.postgres:
            import psycopg

            conn = psycopg.connect(self.url)
        else:
            conn = sqlite3.connect(self.path, timeout=15)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, conn, sql: str, params=()):
        return conn.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def create(self, title: str) -> dict:
        migration = {
            "id": str(uuid4()),
            "title": title,
            "revision": 0,
            "state": "DRAFT",
            "createdAt": now(),
            "updatedAt": now(),
            "specs": {},
            "fixtures": [],
            "plan": None,
            "diff": None,
            "reviews": {},
            "builds": [],
            "model": None,
            "approvalHash": None,
            "activeBuildId": None,
            "sourcePath": None,
            "targetPath": None,
        }
        with self.connection() as conn:
            self.execute(
                conn, "INSERT INTO migrations VALUES (?, ?, ?)", (migration["id"], 0, canonical(migration))
            )
            self.execute(
                conn,
                "INSERT INTO migration_revisions VALUES (?, ?, ?)",
                (migration["id"], 0, canonical(migration)),
            )
            self._audit(conn, migration["id"], "CREATED", {"revision": 0})
            conn.commit()
        return migration

    def get(self, identifier: str) -> dict:
        with self.connection() as conn:
            row = self.execute(conn, "SELECT document FROM migrations WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("Migration not found")
        return json.loads(row[0])

    def list(self) -> list[dict]:
        with self.connection() as conn:
            rows = self.execute(conn, "SELECT document FROM migrations").fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda m: m["createdAt"], reverse=True)

    def mutate(
        self, identifier: str, action: str, visitor: Callable[[dict], None], expected: int | None = None
    ) -> dict:
        with self.lock, self.connection() as conn:
            if not self.postgres:
                conn.execute("BEGIN IMMEDIATE")
            row = self.execute(
                conn,
                "SELECT revision, document FROM migrations WHERE id = ?"
                + (" FOR UPDATE" if self.postgres else ""),
                (identifier,),
            ).fetchone()
            if row is None:
                raise NotFound("Migration not found")
            revision, document = row
            if expected is not None and revision != expected:
                raise Conflict("Revision changed; reload before editing or approving")
            value = json.loads(document)
            visitor(value)
            value.update(revision=revision + 1, updatedAt=now())
            result = self.execute(
                conn,
                "UPDATE migrations SET revision = ?, document = ? WHERE id = ? AND revision = ?",
                (revision + 1, canonical(value), identifier, revision),
            )
            if result.rowcount != 1:
                raise Conflict("Concurrent modification")
            self.execute(
                conn,
                "INSERT INTO migration_revisions VALUES (?, ?, ?)",
                (identifier, revision + 1, canonical(value)),
            )
            self._audit(
                conn,
                identifier,
                action,
                {
                    "revision": revision + 1,
                    "state": value["state"],
                    "planHash": digest(value["plan"]) if value.get("plan") else None,
                    "reviewDecisions": value["reviews"] if action == "PLAN_APPROVED" else None,
                },
            )
            conn.commit()
            return value

    def _audit(self, conn, identifier: str, action: str, metadata: dict):
        event = {
            "id": str(uuid4()),
            "action": action,
            "actor": "local-operator",
            "createdAt": now(),
            "metadata": metadata,
        }
        self.execute(
            conn,
            "INSERT INTO audit_events VALUES (?, ?, ?, ?)",
            (event["id"], identifier, event["createdAt"], canonical(event)),
        )

    def audit(self, identifier: str) -> list[dict]:
        self.get(identifier)
        with self.connection() as conn:
            rows = self.execute(
                conn,
                "SELECT document FROM audit_events WHERE migration_id = ? ORDER BY created_at",
                (identifier,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def job(self, value: dict) -> dict:
        with self.connection() as conn:
            exists = self.execute(conn, "SELECT id FROM jobs WHERE id = ?", (value["id"],)).fetchone()
            if exists:
                self.execute(
                    conn, "UPDATE jobs SET document = ? WHERE id = ?", (canonical(value), value["id"])
                )
            else:
                self.execute(
                    conn,
                    "INSERT INTO jobs VALUES (?, ?, ?)",
                    (value["id"], value["migrationId"], canonical(value)),
                )
            conn.commit()
        return value

    def get_job(self, identifier: str) -> dict:
        with self.connection() as conn:
            row = self.execute(conn, "SELECT document FROM jobs WHERE id = ?", (identifier,)).fetchone()
        if not row:
            raise NotFound("Job not found")
        return json.loads(row[0])

    def recover(self):
        with self.connection() as conn:
            rows = self.execute(conn, "SELECT document FROM jobs").fetchall()
        for row in rows:
            job = json.loads(row[0])
            if job["status"] in ("QUEUED", "RUNNING"):
                job.update(
                    status="FAILED", error="Control plane restarted; rerun this job", completedAt=now()
                )
                self.job(job)
        for m in self.list():
            if m["state"] == "BUILDING" or m.get("pendingJob"):

                def interrupted(value):
                    value["pendingJob"] = None
                    if value["state"] == "BUILDING":
                        value["state"] = "TEST_FAILED"
                    for build in value["builds"]:
                        if build["status"] == "RUNNING":
                            build.update(status="FAILED", error="Interrupted by control-plane restart")

                self.mutate(m["id"], "BUILD_INTERRUPTED", interrupted)
