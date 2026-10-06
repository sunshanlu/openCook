import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import TaskError


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


@contextmanager
def state_lock(directory: Path):
    """Chrome profiles and their cache must have only one writer (macOS v1)."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskError("state_busy", "另一个任务正在使用此状态目录", retryable=False) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.db = sqlite3.connect(directory / "cache.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS aliases (url TEXT PRIMARY KEY, id TEXT NOT NULL)"
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, note: str):
        row = self.db.execute("SELECT value FROM records WHERE id=?", (note,)).fetchone()
        if not row:
            return None
        try:
            record = json.loads(row[0])
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                return None
            if record.get("status") == "success":
                from .models import Extraction, Source

                source = Source.model_validate(record["source"])
                extraction = Extraction.model_validate(record["extraction"])
                names = record["export_names"]
                if source.note_id != note or not isinstance(names, list):
                    return None
                if len(names) != len(extraction.recipes) or any(
                    not isinstance(name, str) or not name.strip() for name in names
                ):
                    return None
            return record
        except (ValueError, TypeError, KeyError):
            return None

    def resolve(self, url: str) -> str | None:
        row = self.db.execute("SELECT id FROM aliases WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

    def alias(self, url: str, note: str):
        self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?)", (url, note))
        self.db.commit()

    def save(self, note: str, record: dict):
        record = {**record, "updated_at": now(), "schema_version": 1}
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?, ?)",
            (note, json.dumps(record, ensure_ascii=False, allow_nan=False)),
        )
        self.db.commit()

    def attempt_directory(self, note: str, attempt: int) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.directory / "raw" / note / f"{stamp}-{attempt}"
        path.mkdir(parents=True)
        return path
