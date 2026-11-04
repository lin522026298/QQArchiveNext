"""Disposable local conversation summaries; never an integrity attestation."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path


def default_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache")))
    return base / "QQArchiveNext" / "viewer-cache"


def binding(manifest: Path, connections) -> str:
    digest = hashlib.sha256(b"qq-conversation-summary-1\0")
    digest.update(manifest.read_bytes())
    for connection in connections:
        if connection is None:
            continue
        database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        for path in (database, Path(str(database) + "-wal")):
            digest.update(str(path.resolve()).encode("utf-8"))
            if path.exists():
                stat = path.stat()
                digest.update(repr((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)).encode())
                with path.open("rb") as stream:
                    digest.update(stream.read(100))
        digest.update(str(connection.execute("PRAGMA schema_version").fetchone()[0]).encode())
    return digest.hexdigest()


def load(directory: Path, key: str, factory):
    path = directory / (key + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024**2:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "qq-conversation-summary/1" or data.get("binding") != key:
            return None
        result = []
        for item in data["conversations"]:
            if item["source_table"] not in {"c2c_messages", "group_messages"}:
                return None
            item["uid_values"] = tuple(item["uid_values"])
            result.append(factory(**item))
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(directory: Path, key: str, values) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("summary cache cannot be a symbolic link")
    text = json.dumps({"schema": "qq-conversation-summary/1", "binding": key,
                       "conversations": [asdict(value) for value in values]}, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     suffix=".partial", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, directory / (key + ".json"))
    finally:
        temporary.unlink(missing_ok=True)
