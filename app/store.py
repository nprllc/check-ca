"""Stockage des rapports : SQLite (CHECKCA_DB).

Sur l'offre gratuite de Render, le disque est éphémère : la base est
effacée à chaque redéploiement (voir README).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    canonical TEXT,
    created_at TEXT,
    json TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_canonical ON reports(canonical);
"""


class Store:
    """Petite base de rapports avec cache par URL canonique (24 h)."""

    def __init__(self, db_path: str = "checkca.sqlite3"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def save(self, report_id: str, canonical: str, data: dict) -> None:
        created = data.get("created_at") or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO reports (id, canonical, created_at, json) VALUES (?, ?, ?, ?)",
            (report_id, canonical, created, json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()

    def get(self, report_id: str) -> dict | None:
        row = self.conn.execute("SELECT json FROM reports WHERE id = ?", (report_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def find_cached(self, canonical: str, max_age_hours: int = 24) -> dict | None:
        """Rapport récent pour cette URL canonique, ou None."""
        row = self.conn.execute(
            "SELECT json, created_at FROM reports WHERE canonical = ? ORDER BY created_at DESC LIMIT 1",
            (canonical,),
        ).fetchone()
        if not row:
            return None
        try:
            created = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(hours=max_age_hours):
                return None
        except (ValueError, TypeError):
            pass
        report = json.loads(row[0])
        report["cached"] = True
        return report
