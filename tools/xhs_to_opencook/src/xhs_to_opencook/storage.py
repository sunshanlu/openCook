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
        for table in (
            "profiles",
            "classifications",
            "extraction_runs",
            "chat_batches",
            "chat_requests",
        ):
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        self.db.execute("""CREATE TABLE IF NOT EXISTS profile_notes (
            profile_id TEXT NOT NULL, note_id TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(profile_id, note_id))""")
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

    def stage_get(self, table: str, identity: str):
        if table not in {
            "profiles",
            "classifications",
            "extraction_runs",
            "chat_batches",
            "chat_requests",
        }:
            raise ValueError("unsupported table")
        row = self.db.execute(f"SELECT value FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None

    def stage_save(self, table: str, identity: str, value: dict):
        if table not in {
            "profiles",
            "classifications",
            "extraction_runs",
            "chat_batches",
            "chat_requests",
        }:
            raise ValueError("unsupported table")
        value = {**value, "updated_at": now()}
        self.db.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",
            (
                identity,
                json.dumps(value, ensure_ascii=False, allow_nan=False),
            ),
        )
        self.db.commit()

    def collect_note(self, profile: str, note: dict):
        note = {k: v for k, v in note.items() if k != "profile_ids"}
        identity = note["note_id"]
        row = self.db.execute(
            "SELECT value FROM profile_notes WHERE profile_id=? AND note_id=?",
            (profile, identity),
        ).fetchone()
        previous = json.loads(row[0]) if row else {}
        for field in ("type", "title", "author", "author_id", "cover_url", "source"):
            if not note.get(field) and previous.get(field):
                note[field] = previous[field]
        value = {
            **previous,
            **note,
            "first_seen": previous.get("first_seen", now()),
            "last_seen": now(),
        }
        self.db.execute(
            "INSERT OR REPLACE INTO profile_notes VALUES (?, ?, ?)",
            (
                profile,
                identity,
                json.dumps(value, ensure_ascii=False, allow_nan=False),
            ),
        )
        self.alias(note["url"], identity)

    def collected(self, profiles: list[str]) -> list[dict]:
        result = {}
        for profile in profiles:
            rows = self.db.execute(
                "SELECT note_id, value FROM profile_notes WHERE profile_id=? "
                "ORDER BY json_extract(value, '$.first_seen'), note_id",
                (profile,),
            )
            for identity, value in rows:
                note = json.loads(value)
                if identity not in result:
                    result[identity] = {**note, "profile_ids": []}
                elif note["last_seen"] > result[identity]["last_seen"]:
                    result[identity].update({k: v for k, v in note.items() if k != "profile_ids"})
                result[identity]["profile_ids"].append(profile)
        return list(result.values())

    def stage_rows(self, table: str) -> list[dict]:
        if table not in {"chat_batches", "chat_requests"}:
            raise ValueError("unsupported table")
        return [
            json.loads(row[0])
            for row in self.db.execute(f"SELECT value FROM {table} ORDER BY rowid")
        ]

    def replace_chat_request(self, previous: dict, following: dict):
        """Atomically retire an invalid answer and reserve its immediate correction."""
        with self.db:
            for request in (previous, following):
                self.db.execute(
                    "INSERT OR REPLACE INTO chat_requests VALUES (?, ?)",
                    (
                        request["request_id"],
                        json.dumps(request, ensure_ascii=False, allow_nan=False),
                    ),
                )

    def seal_chat_request(self, request: dict, batch: dict):
        """Commit a sealed batch and its recovery intent as one durable transition."""
        if request["batch_id"] != batch["batch_id"] or batch["status"] != "sealed":
            raise ValueError("recovery request must belong to the sealed batch")
        stamp = now()
        rows = [
            (table, identity, json.dumps({**value, "updated_at": stamp},
                                         ensure_ascii=False, allow_nan=False))
            for table, identity, value in (
                ("chat_batches", batch["batch_id"], batch),
                ("chat_requests", request["request_id"], request),
            )
        ]
        with self.db:
            for table, identity, value in rows:
                self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",
                                (identity, value))
