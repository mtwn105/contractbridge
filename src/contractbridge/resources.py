"""Revisioned runtime connections/incidents. No request or response payload storage."""

from __future__ import annotations

import json
from uuid import uuid4

from .storage import Conflict, NotFound, now
from .util import canonical


class Resources:
    def __init__(self, store):
        self.store = store
        with store.connection() as conn:
            store.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS runtime_resources (kind TEXT NOT NULL, id TEXT NOT NULL, revision BIGINT NOT NULL, document TEXT NOT NULL, PRIMARY KEY (kind, id))",
            )
            store.execute(
                conn,
                "CREATE TABLE IF NOT EXISTS runtime_events (id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, document TEXT NOT NULL)",
            )
            conn.commit()

    def get(self, kind, identifier):
        with self.store.connection() as conn:
            row = self.store.execute(
                conn, "SELECT document FROM runtime_resources WHERE kind = ? AND id = ?", (kind, identifier)
            ).fetchone()
        if row is None:
            raise NotFound("Runtime resource not found")
        return json.loads(row[0])

    def list(self, kind):
        with self.store.connection() as conn:
            rows = self.store.execute(
                conn, "SELECT document FROM runtime_resources WHERE kind = ?", (kind,)
            ).fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda r: r["createdAt"], reverse=True)

    def create(self, kind, data, identifier=None):
        value = {
            **data,
            "id": identifier or str(uuid4()),
            "revision": 0,
            "createdAt": now(),
            "updatedAt": now(),
        }
        with self.store.lock, self.store.connection() as conn:
            self.store.execute(
                conn,
                "INSERT INTO runtime_resources VALUES (?, ?, ?, ?)",
                (kind, value["id"], 0, canonical(value)),
            )
            self._event(conn, value["id"], "CREATED", 0, value)
            conn.commit()
        return value

    def update(self, kind, identifier, action, visitor, expected=None):
        with self.store.lock, self.store.connection() as conn:
            if not self.store.postgres:
                conn.execute("BEGIN IMMEDIATE")
            row = self.store.execute(
                conn,
                "SELECT revision, document FROM runtime_resources WHERE kind = ? AND id = ?"
                + (" FOR UPDATE" if self.store.postgres else ""),
                (kind, identifier),
            ).fetchone()
            if row is None:
                raise NotFound("Runtime resource not found")
            revision, document = row
            if expected is not None and expected != revision:
                raise Conflict("Runtime resource changed; reload before acting")
            value = json.loads(document)
            visitor(value)
            value.update(revision=revision + 1, updatedAt=now())
            result = self.store.execute(
                conn,
                "UPDATE runtime_resources SET revision = ?, document = ? WHERE kind = ? AND id = ? AND revision = ?",
                (revision + 1, canonical(value), kind, identifier, revision),
            )
            if result.rowcount != 1:
                raise Conflict("Concurrent runtime update")
            self._event(conn, identifier, action, revision + 1, value)
            conn.commit()
        return value

    def _event(self, conn, identifier, action, revision, value):
        event = {
            "id": str(uuid4()),
            "resourceId": identifier,
            "action": action,
            "revision": revision,
            "createdAt": now(),
            "metadata": {
                "autoPublishDraft": value.get("autoPublishDraft"),
                "publicationAuthorization": value.get("publicationAuthorization"),
                "candidateArtifactHash": (value.get("candidate") or {}).get("artifactHash"),
                "publicationAuthorizationType": (value.get("publication") or {}).get("authorization"),
            },
        }
        self.store.execute(
            conn, "INSERT INTO runtime_events VALUES (?, ?, ?)", (event["id"], identifier, canonical(event))
        )

    def audit(self, identifier):
        with self.store.connection() as conn:
            rows = self.store.execute(
                conn, "SELECT document FROM runtime_events WHERE resource_id = ?", (identifier,)
            ).fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda r: r["createdAt"])
