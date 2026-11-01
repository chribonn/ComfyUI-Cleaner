"""Local SQLite database, so models are only looked up online once.

Tables
  verified_models  models confirmed on Hugging Face / Civitai (never re-checked)
  failed_lookups   models not found online (re-checked after a few days)
  file_hashes      SHA-256 of local files, keyed by path + size + modified time
  hf_author_index  file lists of the Hugging Face accounts in the config
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verified_models (
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT,
    source      TEXT NOT NULL,
    repo        TEXT,
    remote_path TEXT,
    url         TEXT,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (kind, name, size)
);
CREATE INDEX IF NOT EXISTS idx_verified_sha ON verified_models(sha256);
CREATE TABLE IF NOT EXISTS failed_lookups (
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    status     TEXT NOT NULL,
    note       TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (kind, name, size)
);
CREATE TABLE IF NOT EXISTS file_hashes (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    sha256    TEXT NOT NULL,
    hashed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hf_author_index (
    author    TEXT NOT NULL,
    filename  TEXT NOT NULL,
    repo      TEXT NOT NULL,
    rfilename TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hf_index_author ON hf_author_index(author);
CREATE TABLE IF NOT EXISTS hf_author_fetch (
    author     TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


class ModelDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ModelDatabase":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- verified models -------------------------------------------------
    def get_verified(self, kind: str, name: str, size: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM verified_models WHERE kind=? AND name=? AND size=?",
            (kind, name.lower(), size)).fetchone()

    def get_verified_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM verified_models WHERE sha256=? LIMIT 1", (sha256.lower(),)).fetchone()

    def put_verified(self, kind: str, name: str, size: int, *, source: str, repo: str,
                     url: str, remote_path: str = "", sha256: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO verified_models "
                "(kind, name, size, sha256, source, repo, remote_path, url, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, name.lower(), size, sha256.lower() or None, source, repo,
                 remote_path, url, _now()))
            self.conn.execute(
                "DELETE FROM failed_lookups WHERE kind=? AND name=? AND size=?",
                (kind, name.lower(), size))

    # --- failed lookups --------------------------------------------------
    def get_failed(self, kind: str, name: str, size: int) -> tuple[str, str, datetime] | None:
        """(status, note, checked_at) of the last unsuccessful lookup, if any."""
        row = self.conn.execute(
            "SELECT status, note, checked_at FROM failed_lookups WHERE kind=? AND name=? AND size=?",
            (kind, name.lower(), size)).fetchone()
        if not row:
            return None
        checked = _parse(row["checked_at"]) or datetime.fromtimestamp(0, timezone.utc)
        return row["status"], row["note"] or "", checked

    def put_failed(self, kind: str, name: str, size: int, status: str, note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO failed_lookups (kind, name, size, status, note, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (kind, name.lower(), size, status, note, _now()))

    # --- file hashes -----------------------------------------------------
    def get_hash(self, path: Path, size: int, mtime_ns: int) -> str | None:
        row = self.conn.execute(
            "SELECT sha256 FROM file_hashes WHERE path=? AND size=? AND mtime_ns=?",
            (_path_key(path), size, mtime_ns)).fetchone()
        return row["sha256"] if row else None

    def put_hash(self, path: Path, size: int, mtime_ns: int, sha256: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO file_hashes (path, size, mtime_ns, sha256, hashed_at) "
                "VALUES (?, ?, ?, ?, ?)", (_path_key(path), size, mtime_ns, sha256.lower(), _now()))

    # --- Hugging Face author index ---------------------------------------
    def get_author_index(self, author: str) -> tuple[datetime | None, list[tuple[str, str, str]]]:
        row = self.conn.execute(
            "SELECT fetched_at FROM hf_author_fetch WHERE author=?", (author,)).fetchone()
        rows = self.conn.execute(
            "SELECT filename, repo, rfilename FROM hf_author_index WHERE author=?",
            (author,)).fetchall()
        return (_parse(row["fetched_at"]) if row else None,
                [(r["filename"], r["repo"], r["rfilename"]) for r in rows])

    def replace_author_index(self, author: str, rows: list[tuple[str, str, str]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM hf_author_index WHERE author=?", (author,))
            self.conn.executemany(
                "INSERT INTO hf_author_index (author, filename, repo, rfilename) VALUES (?, ?, ?, ?)",
                [(author, *row) for row in rows])
            self.conn.execute(
                "INSERT OR REPLACE INTO hf_author_fetch (author, fetched_at) VALUES (?, ?)",
                (author, _now()))
