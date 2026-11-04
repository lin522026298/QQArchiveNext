#!/usr/bin/env python3
"""DEPRECATED: build a legacy qq-canonical-publication/1.

The publication contains one logical message per conservative warehouse fold,
complete observation provenance, per-account media/identity views, and one
publication-wide SHA-256 object directory.  Media paths inside each account are
hardlinks to that shared object directory, so the v1 viewer contract remains
portable without copying content bytes on the working volume.

New publications must use Build-QqCanonicalPublicationV2.py.  This frozen v1
builder remains only for reproducibility and requires an explicit legacy flag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator


PUBLICATION_SCHEMA = "qq-canonical-publication/1"
MESSAGE_SCHEMA = "qq-canonical-message-view/1"
MEDIA_SCHEMA = "qq-media-map/2"
IDENTITY_SCHEMA = "qq-identity-index/1"
RAW_TO_STRUCTURED = {
    "c2c_msg_table": "c2c_messages",
    "group_msg_table": "group_messages",
}
SOURCE_PRIORITY = {
    "official_windows_msgbackup_restored_on_android_vm": 0,
    "legacy_import": 1,
    "android_root_snapshot": 2,
    "windows_ntqq_snapshot": 3,
}
VERIFICATION_STRENGTH = {
    "verified_content": 500,
    "preview_only": 400,
    "filename_candidate": 300,
    "path_candidate": 250,
    "type_conflict_quarantined": 100,
    "no_exact_local_content": 0,
}
ASSET_PREFIXES = ("primary", "preview", "playable", "candidate")


def stop(message: str) -> None:
    raise SystemExit(message)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        stop(f"JSON root is not an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def ro_uri(path: Path) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except ValueError:
        return False


def stable_message_id(identity_digest: bytes) -> int:
    value = int.from_bytes(identity_digest[:8], "big") & ((1 << 63) - 1)
    return value or 1


def identity_digest(observation_key: bytes, logical_id: str | None) -> bytes:
    if logical_id:
        return hashlib.sha256(b"qq-published-logical-v1\0" + logical_id.encode()).digest()
    return hashlib.sha256(b"qq-published-observation-v1\0" + observation_key).digest()


def batches(rows: Iterable[tuple[Any, ...]], size: int = 10_000) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def init_message_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE source_runs(
          account_run_id TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          package_id TEXT NOT NULL,
          structured_relative TEXT NOT NULL,
          structured_sha256 TEXT NOT NULL,
          selected_priority INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE logical_messages(
          published_msg_id INTEGER PRIMARY KEY,
          identity_digest BLOB NOT NULL UNIQUE CHECK(length(identity_digest)=32),
          source_table TEXT NOT NULL,
          selected_account_run_id TEXT NOT NULL,
          selected_source_msg_id INTEGER NOT NULL,
          member_count INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(selected_account_run_id) REFERENCES source_runs(account_run_id)
        );
        CREATE TABLE message_provenance(
          published_msg_id INTEGER NOT NULL REFERENCES logical_messages(published_msg_id),
          account_run_id TEXT NOT NULL REFERENCES source_runs(account_run_id),
          source_table TEXT NOT NULL,
          source_msg_id INTEGER NOT NULL,
          observation_key BLOB NOT NULL UNIQUE CHECK(length(observation_key)=32),
          row_digest BLOB NOT NULL CHECK(length(row_digest)=32),
          selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
          PRIMARY KEY(account_run_id,source_table,source_msg_id)
        );
        CREATE INDEX provenance_by_published ON message_provenance(published_msg_id);
        CREATE TABLE c2c_messages (
          msg_id INTEGER PRIMARY KEY,
          timestamp INTEGER NOT NULL,
          direction INTEGER NOT NULL,
          sender_uid TEXT NOT NULL,
          sender_qq INTEGER,
          peer_uid TEXT NOT NULL,
          peer_qq INTEGER NOT NULL,
          msg_type INTEGER NOT NULL,
          content_type INTEGER,
          proto_ver TEXT,
          inner_ts INTEGER,
          text TEXT,
          content TEXT
        );
        CREATE TABLE group_messages (
          msg_id INTEGER PRIMARY KEY,
          timestamp INTEGER NOT NULL,
          direction INTEGER NOT NULL,
          sender_uid TEXT NOT NULL,
          sender_qq INTEGER,
          group_id TEXT NOT NULL,
          group_qq INTEGER NOT NULL,
          msg_type INTEGER NOT NULL,
          subtype INTEGER,
          content_type INTEGER,
          text TEXT,
          parse_status TEXT NOT NULL,
          content TEXT
        );
        CREATE INDEX c2c_conversation ON c2c_messages(peer_qq,peer_uid,timestamp,msg_id);
        CREATE INDEX group_conversation ON group_messages(group_qq,group_id,timestamp,msg_id);
        """
    )


def register_source_runs(
    connection: sqlite3.Connection, runs: list[sqlite3.Row]
) -> None:
    connection.executemany(
        "INSERT INTO source_runs VALUES (?,?,?,?,?,?)",
        [
            (
                row["account_run_id"],
                row["source_kind"],
                row["package_id"],
                row["structured_relative"],
                row["structured_sha256"],
                SOURCE_PRIORITY.get(row["source_kind"], 99),
            )
            for row in runs
        ],
    )


def build_provenance(
    connection: sqlite3.Connection,
    observation_path: Path,
    runs: list[sqlite3.Row],
) -> None:
    observation = sqlite3.connect(ro_uri(observation_path), uri=True)
    observation.row_factory = sqlite3.Row
    try:
        for run in runs:
            cursor = observation.execute(
                """
                SELECT o.observation_key,o.source_table,o.msg_id,o.row_digest,
                       lm.logical_id
                  FROM message_observations AS o
                  LEFT JOIN logical_members AS lm
                    ON lm.observation_key=o.observation_key
                 WHERE o.account_run_id=?
                 ORDER BY o.source_table,o.msg_id
                """,
                (run["account_run_id"],),
            )
            for chunk in batches(
                (
                    (
                        stable_message_id(
                            identity_digest(bytes(row["observation_key"]), row["logical_id"])
                        ),
                        identity_digest(bytes(row["observation_key"]), row["logical_id"]),
                        row["source_table"],
                        run["account_run_id"],
                        int(row["msg_id"]),
                        run["account_run_id"],
                        row["source_table"],
                        int(row["msg_id"]),
                        bytes(row["observation_key"]),
                        bytes(row["row_digest"]),
                    )
                    for row in cursor
                )
            ):
                connection.executemany(
                    """
                    INSERT INTO logical_messages(
                      published_msg_id,identity_digest,source_table,
                      selected_account_run_id,selected_source_msg_id
                    ) VALUES (?,?,?,?,?)
                    ON CONFLICT(identity_digest) DO NOTHING
                    """,
                    [row[:5] for row in chunk],
                )
                connection.executemany(
                    """
                    INSERT INTO message_provenance(
                      published_msg_id,account_run_id,source_table,source_msg_id,
                      observation_key,row_digest
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    [
                        (row[0], row[5], row[6], row[7], row[8], row[9])
                        for row in chunk
                    ],
                )
                connection.commit()
        collision = connection.execute(
            """
            SELECT count(*) FROM message_provenance AS p
            LEFT JOIN logical_messages AS l ON l.published_msg_id=p.published_msg_id
            WHERE l.published_msg_id IS NULL
            """
        ).fetchone()[0]
        if int(collision):
            stop("stable published message ID collision detected")
        mixed = connection.execute(
            """
            SELECT count(*) FROM logical_messages AS l
            JOIN message_provenance AS p ON p.published_msg_id=l.published_msg_id
            WHERE p.source_table<>l.source_table
            """
        ).fetchone()[0]
        if int(mixed):
            stop("a logical message crossed raw source-table types")
        connection.execute(
            """
            UPDATE logical_messages
               SET member_count=(SELECT count(*) FROM message_provenance AS p
                                  WHERE p.published_msg_id=logical_messages.published_msg_id)
            """
        )
        connection.execute(
            """
            UPDATE message_provenance
               SET selected=1
             WHERE EXISTS(
               SELECT 1 FROM logical_messages AS l
                WHERE l.published_msg_id=message_provenance.published_msg_id
                  AND l.selected_account_run_id=message_provenance.account_run_id
                  AND l.source_table=message_provenance.source_table
                  AND l.selected_source_msg_id=message_provenance.source_msg_id
             )
            """
        )
        connection.commit()
    finally:
        observation.close()


def copy_selected_messages(
    connection: sqlite3.Connection, project: Path, runs: list[sqlite3.Row]
) -> None:
    for run in runs:
        structured = (project / run["structured_relative"]).resolve(strict=True)
        connection.execute("ATTACH DATABASE ? AS src", (ro_uri(structured),))
        try:
            connection.execute(
                """
                INSERT INTO c2c_messages
                SELECT p.published_msg_id,s.timestamp,s.direction,s.sender_uid,
                       s.sender_qq,s.peer_uid,s.peer_qq,s.msg_type,s.content_type,
                       s.proto_ver,s.inner_ts,s.text,s.content
                  FROM src.c2c_messages AS s
                  JOIN message_provenance AS p
                    ON p.account_run_id=? AND p.source_table='c2c_messages'
                   AND p.source_msg_id=s.msg_id AND p.selected=1
                """,
                (run["account_run_id"],),
            )
            connection.execute(
                """
                INSERT INTO group_messages
                SELECT p.published_msg_id,s.timestamp,s.direction,s.sender_uid,
                       s.sender_qq,s.group_id,s.group_qq,s.msg_type,s.subtype,
                       s.content_type,s.text,s.parse_status,s.content
                  FROM src.group_messages AS s
                  JOIN message_provenance AS p
                    ON p.account_run_id=? AND p.source_table='group_messages'
                   AND p.source_msg_id=s.msg_id AND p.selected=1
                """,
                (run["account_run_id"],),
            )
            connection.commit()
        finally:
            connection.execute("DETACH DATABASE src")
    expected = int(connection.execute("SELECT count(*) FROM logical_messages").fetchone()[0])
    actual = int(
        connection.execute(
            "SELECT (SELECT count(*) FROM c2c_messages)+(SELECT count(*) FROM group_messages)"
        ).fetchone()[0]
    )
    if actual != expected:
        stop(f"selected message conservation failed: {actual} != {expected}")


def build_fts(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE VIRTUAL TABLE c2c_messages_fts USING fts5(
          text,content='c2c_messages',content_rowid='msg_id',tokenize='unicode61'
        );
        CREATE VIRTUAL TABLE group_messages_fts USING fts5(
          text,content='group_messages',content_rowid='msg_id',tokenize='unicode61'
        );
        INSERT INTO c2c_messages_fts(rowid,text) SELECT msg_id,coalesce(text,'') FROM c2c_messages;
        INSERT INTO group_messages_fts(rowid,text) SELECT msg_id,coalesce(text,'') FROM group_messages;
        """
    )


def message_summary(connection: sqlite3.Connection) -> dict[str, int]:
    values = {
        "c2c_messages": int(connection.execute("SELECT count(*) FROM c2c_messages").fetchone()[0]),
        "group_messages": int(connection.execute("SELECT count(*) FROM group_messages").fetchone()[0]),
        "observations": int(connection.execute("SELECT count(*) FROM message_provenance").fetchone()[0]),
        "logical_messages": int(connection.execute("SELECT count(*) FROM logical_messages").fetchone()[0]),
        "strict_fold_groups": int(connection.execute("SELECT count(*) FROM logical_messages WHERE member_count>1").fetchone()[0]),
        "folded_observations": int(connection.execute("SELECT coalesce(sum(member_count),0) FROM logical_messages WHERE member_count>1").fetchone()[0]),
    }
    values["removed_duplicate_observations"] = values["observations"] - values["logical_messages"]
    return values


def init_media_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE assets(
          asset_id INTEGER PRIMARY KEY,
          asset_kind TEXT NOT NULL,
          source_relative TEXT,
          archive_relative TEXT NOT NULL UNIQUE,
          byte_count INTEGER NOT NULL,
          sha256 TEXT NOT NULL UNIQUE,
          md5 TEXT,
          detected_mime TEXT NOT NULL,
          cache_class TEXT NOT NULL,
          original_name TEXT,
          verification TEXT NOT NULL,
          mime_evidence TEXT NOT NULL,
          hash_evidence TEXT NOT NULL
        );
        CREATE TABLE message_media(
          link_id INTEGER PRIMARY KEY,
          source_table TEXT NOT NULL,
          msg_id INTEGER NOT NULL,
          segment_index INTEGER NOT NULL,
          conversation_kind TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          timestamp INTEGER,
          direction INTEGER,
          sender_id TEXT,
          msg_type INTEGER NOT NULL,
          content_type INTEGER,
          media_type TEXT NOT NULL,
          role TEXT NOT NULL,
          primary_md5 TEXT,
          thumbnail_md5 TEXT,
          filename TEXT,
          file_uuid TEXT,
          primary_asset_id INTEGER REFERENCES assets(asset_id),
          preview_asset_id INTEGER REFERENCES assets(asset_id),
          playable_asset_id INTEGER REFERENCES assets(asset_id),
          candidate_asset_id INTEGER REFERENCES assets(asset_id),
          verification TEXT NOT NULL,
          note TEXT NOT NULL,
          match_method TEXT NOT NULL,
          type_gate TEXT NOT NULL,
          mapping_rule_version TEXT NOT NULL,
          legacy_verification TEXT NOT NULL,
          legacy_role TEXT NOT NULL,
          strength INTEGER NOT NULL,
          selected_account_run_id TEXT NOT NULL,
          selected_source_link_id INTEGER NOT NULL,
          UNIQUE(source_table,msg_id,segment_index)
        );
        CREATE TABLE media_provenance(
          account_run_id TEXT NOT NULL,
          source_link_id INTEGER NOT NULL,
          published_msg_id INTEGER NOT NULL,
          source_table TEXT NOT NULL,
          segment_index INTEGER NOT NULL,
          media_type TEXT NOT NULL,
          verification TEXT NOT NULL,
          primary_sha256 TEXT,
          preview_sha256 TEXT,
          playable_sha256 TEXT,
          candidate_sha256 TEXT,
          selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
          PRIMARY KEY(account_run_id,source_link_id)
        );
        CREATE INDEX message_media_selected_source
          ON message_media(selected_account_run_id,selected_source_link_id);
        CREATE INDEX media_provenance_published ON media_provenance(published_msg_id);
        """
    )


def asset_columns(prefix: str) -> str:
    alias = f"a_{prefix}"
    return ",".join(
        f"{alias}.{column} AS {prefix}_{column}"
        for column in (
            "asset_id", "asset_kind", "archive_relative", "byte_count", "sha256",
            "md5", "detected_mime", "cache_class", "original_name", "verification",
            "mime_evidence", "hash_evidence",
        )
    )


def materialize_asset(
    row: sqlite3.Row,
    prefix: str,
    source_archive: Path,
    publication_root: Path,
    account_archive: Path,
    warehouse: Path,
    warehouse_objects: dict[str, str],
    object_sizes: dict[str, int],
    media_connection: sqlite3.Connection,
    account_assets: dict[str, int],
) -> int | None:
    if row[f"{prefix}_asset_id"] is None:
        return None
    sha = str(row[f"{prefix}_sha256"]).lower()
    byte_count = int(row[f"{prefix}_byte_count"])
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        stop("source media asset has an invalid SHA-256")
    existing_size = object_sizes.get(sha)
    if existing_size is not None and existing_size != byte_count:
        stop("same publication SHA-256 has inconsistent sizes")
    object_relative = Path("objects") / "sha256" / sha[:2] / sha
    publication_object = publication_root / object_relative
    if existing_size is None:
        canonical_relative = warehouse_objects.get(sha)
        if canonical_relative:
            source = (warehouse / canonical_relative).resolve(strict=True)
        else:
            source = (source_archive / str(row[f"{prefix}_archive_relative"])).resolve(strict=True)
            if not inside(source, source_archive):
                stop("source media asset escaped its archive")
        if source.stat().st_size != byte_count:
            stop("source media asset size does not match its index")
        publication_object.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, publication_object)
        object_sizes[sha] = byte_count
    account_relative = Path("media") / "sha256" / sha[:2] / sha
    account_object = account_archive / account_relative
    if sha not in account_assets:
        account_object.parent.mkdir(parents=True, exist_ok=True)
        os.link(publication_object, account_object)
        cursor = media_connection.execute(
            """
            INSERT INTO assets(
              asset_kind,source_relative,archive_relative,byte_count,sha256,md5,
              detected_mime,cache_class,original_name,verification,mime_evidence,hash_evidence
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row[f"{prefix}_asset_kind"],
                f"canonical_sha256:{sha}",
                account_relative.as_posix(),
                byte_count,
                sha,
                row[f"{prefix}_md5"],
                row[f"{prefix}_detected_mime"],
                row[f"{prefix}_cache_class"],
                row[f"{prefix}_original_name"],
                row[f"{prefix}_verification"],
                row[f"{prefix}_mime_evidence"],
                row[f"{prefix}_hash_evidence"],
            ),
        )
        account_assets[sha] = int(cursor.lastrowid)
    return account_assets[sha]


def build_media_view(
    project: Path,
    warehouse: Path,
    publication_root: Path,
    account_archive: Path,
    message_path: Path,
    runs: list[sqlite3.Row],
    warehouse_objects: dict[str, str],
    object_sizes: dict[str, int],
) -> dict[str, int]:
    media_path = account_archive / "index" / "media_map.sqlite"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media = sqlite3.connect(media_path, timeout=120, uri=True)
    media.row_factory = sqlite3.Row
    init_media_database(media)
    media.execute("ATTACH DATABASE ? AS msg", (ro_uri(message_path),))
    account_assets: dict[str, int] = {}
    source_links = 0
    try:
        for run in runs:
            map_path = (project / run["media_map_relative"]).resolve(strict=True)
            source_archive = map_path.parent.parent
            media.execute("ATTACH DATABASE ? AS source_map", (ro_uri(map_path),))
            joins = []
            selects = []
            for prefix, slot in zip(ASSET_PREFIXES, ("primary", "preview", "playable", "candidate")):
                alias = f"a_{prefix}"
                joins.append(f"LEFT JOIN source_map.assets AS {alias} ON {alias}.asset_id=m.{slot}_asset_id")
                selects.append(asset_columns(prefix))
            query = f"""
                SELECT p.published_msg_id,m.*, {','.join(selects)}
                  FROM source_map.message_media AS m
                  JOIN msg.message_provenance AS p
                    ON p.account_run_id=?
                   AND p.source_table=CASE m.source_table
                         WHEN 'c2c_msg_table' THEN 'c2c_messages'
                         WHEN 'group_msg_table' THEN 'group_messages'
                       END
                   AND p.source_msg_id=m.msg_id
                  {' '.join(joins)}
                 ORDER BY m.link_id
            """
            try:
                for row in media.execute(query, (run["account_run_id"],)):
                    asset_ids = [
                        materialize_asset(
                            row, prefix, source_archive, publication_root, account_archive,
                            warehouse, warehouse_objects, object_sizes, media, account_assets,
                        )
                        for prefix in ASSET_PREFIXES
                    ]
                    strength = VERIFICATION_STRENGTH.get(str(row["verification"]), 0)
                    strength += sum(value is not None for value in asset_ids)
                    fields = (
                        row["source_table"], int(row["published_msg_id"]), int(row["segment_index"]),
                        row["conversation_kind"], row["conversation_id"], row["timestamp"],
                        row["direction"], row["sender_id"], int(row["msg_type"]),
                        row["content_type"], row["media_type"], row["role"], row["primary_md5"],
                        row["thumbnail_md5"], row["filename"], row["file_uuid"], *asset_ids,
                        row["verification"], row["note"], row["match_method"], row["type_gate"],
                        row["mapping_rule_version"], row["legacy_verification"], row["legacy_role"],
                        strength, run["account_run_id"], int(row["link_id"]),
                    )
                    media.execute(
                        """
                        INSERT INTO message_media(
                          source_table,msg_id,segment_index,conversation_kind,conversation_id,
                          timestamp,direction,sender_id,msg_type,content_type,media_type,role,
                          primary_md5,thumbnail_md5,filename,file_uuid,primary_asset_id,
                          preview_asset_id,playable_asset_id,candidate_asset_id,verification,note,
                          match_method,type_gate,mapping_rule_version,legacy_verification,legacy_role,
                          strength,selected_account_run_id,selected_source_link_id
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_table,msg_id,segment_index) DO UPDATE SET
                          conversation_kind=excluded.conversation_kind,
                          conversation_id=excluded.conversation_id,timestamp=excluded.timestamp,
                          direction=excluded.direction,sender_id=excluded.sender_id,msg_type=excluded.msg_type,
                          content_type=excluded.content_type,media_type=excluded.media_type,role=excluded.role,
                          primary_md5=excluded.primary_md5,thumbnail_md5=excluded.thumbnail_md5,
                          filename=excluded.filename,file_uuid=excluded.file_uuid,
                          primary_asset_id=excluded.primary_asset_id,preview_asset_id=excluded.preview_asset_id,
                          playable_asset_id=excluded.playable_asset_id,candidate_asset_id=excluded.candidate_asset_id,
                          verification=excluded.verification,note=excluded.note,match_method=excluded.match_method,
                          type_gate=excluded.type_gate,mapping_rule_version=excluded.mapping_rule_version,
                          legacy_verification=excluded.legacy_verification,legacy_role=excluded.legacy_role,
                          strength=excluded.strength,selected_account_run_id=excluded.selected_account_run_id,
                          selected_source_link_id=excluded.selected_source_link_id
                        WHERE excluded.strength>message_media.strength
                        """,
                        fields,
                    )
                    media.execute(
                        """
                        INSERT INTO media_provenance(
                          account_run_id,source_link_id,published_msg_id,source_table,segment_index,
                          media_type,verification,primary_sha256,preview_sha256,playable_sha256,candidate_sha256
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            run["account_run_id"], int(row["link_id"]), int(row["published_msg_id"]),
                            row["source_table"], int(row["segment_index"]), row["media_type"],
                            row["verification"], *[row[f"{prefix}_sha256"] for prefix in ASSET_PREFIXES],
                        ),
                    )
                    source_links += 1
                    if source_links % 10_000 == 0:
                        media.commit()
                media.commit()
            finally:
                media.execute("DETACH DATABASE source_map")
        media.execute(
            """
            UPDATE media_provenance
               SET selected=1
             WHERE (account_run_id,source_link_id) IN (
               SELECT selected_account_run_id,selected_source_link_id
                 FROM message_media
             )
            """
        )
        media.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            [
                ("schema", json.dumps(MEDIA_SCHEMA)),
                ("publication_schema", json.dumps(PUBLICATION_SCHEMA)),
                ("dedupe_policy", json.dumps("strongest_verified_segment_per_logical_message")),
                ("source_media_observations", json.dumps(source_links)),
            ],
        )
        media.commit()
        media.execute("PRAGMA optimize")
        counts = {
            "source_media_links": source_links,
            "published_media_links": int(media.execute("SELECT count(*) FROM message_media").fetchone()[0]),
            "published_assets": int(media.execute("SELECT count(*) FROM assets").fetchone()[0]),
            "verified_content_links": int(media.execute("SELECT count(*) FROM message_media WHERE verification='verified_content'").fetchone()[0]),
            "preview_only_links": int(media.execute("SELECT count(*) FROM message_media WHERE verification='preview_only'").fetchone()[0]),
            "candidate_links": int(media.execute("SELECT count(*) FROM message_media WHERE verification IN ('filename_candidate','path_candidate')").fetchone()[0]),
            "unavailable_links": int(media.execute("SELECT count(*) FROM message_media WHERE verification='no_exact_local_content'").fetchone()[0]),
            "type_conflicts": int(media.execute("SELECT count(*) FROM message_media WHERE verification='type_conflict_quarantined'").fetchone()[0]),
        }
    finally:
        media.close()
    return counts


def init_identity_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE coverage(
          scope TEXT PRIMARY KEY,total_structured_messages INTEGER NOT NULL,
          raw_messages INTEGER NOT NULL,joined_messages INTEGER NOT NULL,
          labelled_messages INTEGER NOT NULL,group_card_messages INTEGER NOT NULL,
          nickname_fallback_messages INTEGER NOT NULL,unlabelled_messages INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE label_values(
          label_id INTEGER PRIMARY KEY,display_name TEXT NOT NULL,label_source TEXT NOT NULL,
          UNIQUE(display_name,label_source)
        );
        CREATE TABLE group_message_labels(
          msg_id INTEGER PRIMARY KEY,label_id INTEGER NOT NULL REFERENCES label_values(label_id)
        );
        CREATE TABLE identity_provenance(
          published_msg_id INTEGER NOT NULL,account_run_id TEXT NOT NULL,
          source_msg_id INTEGER NOT NULL,label_id INTEGER NOT NULL REFERENCES label_values(label_id),
          selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
          PRIMARY KEY(account_run_id,source_msg_id)
        );
        CREATE INDEX identity_provenance_published_label
          ON identity_provenance(published_msg_id,label_id,account_run_id,source_msg_id);
        """
    )


def build_identity_view(
    project: Path,
    identity_path: Path,
    message_path: Path,
    runs: list[sqlite3.Row],
    display_id: str,
) -> dict[str, int]:
    identity = sqlite3.connect(identity_path, timeout=120, uri=True)
    identity.row_factory = sqlite3.Row
    init_identity_database(identity)
    identity.execute("ATTACH DATABASE ? AS msg", (ro_uri(message_path),))
    try:
        for run in runs:
            source_identity = (
                project / run["media_map_relative"]
            ).resolve(strict=True).with_name("identity_index.sqlite")
            if not source_identity.is_file():
                continue
            identity.execute("ATTACH DATABASE ? AS source_identity", (ro_uri(source_identity),))
            try:
                identity.execute(
                    """
                    INSERT OR IGNORE INTO label_values(display_name,label_source)
                    SELECT DISTINCT v.display_name,v.label_source
                      FROM source_identity.group_message_labels AS g
                      JOIN source_identity.label_values AS v ON v.label_id=g.label_id
                      JOIN msg.message_provenance AS p
                        ON p.account_run_id=? AND p.source_table='group_messages'
                       AND p.source_msg_id=g.msg_id
                    """,
                    (run["account_run_id"],),
                )
                identity.execute(
                    """
                    INSERT OR IGNORE INTO group_message_labels(msg_id,label_id)
                    SELECT p.published_msg_id,target.label_id
                      FROM source_identity.group_message_labels AS g
                      JOIN source_identity.label_values AS v ON v.label_id=g.label_id
                      JOIN label_values AS target
                        ON target.display_name=v.display_name
                       AND target.label_source=v.label_source
                      JOIN msg.message_provenance AS p
                        ON p.account_run_id=? AND p.source_table='group_messages'
                       AND p.source_msg_id=g.msg_id
                     ORDER BY g.msg_id
                    """,
                    (run["account_run_id"],),
                )
                identity.execute(
                    """
                    INSERT INTO identity_provenance(
                      published_msg_id,account_run_id,source_msg_id,label_id,selected
                    )
                    SELECT p.published_msg_id,?,p.source_msg_id,target.label_id,0
                      FROM source_identity.group_message_labels AS g
                      JOIN source_identity.label_values AS v ON v.label_id=g.label_id
                      JOIN label_values AS target
                        ON target.display_name=v.display_name
                       AND target.label_source=v.label_source
                      JOIN msg.message_provenance AS p
                        ON p.account_run_id=? AND p.source_table='group_messages'
                       AND p.source_msg_id=g.msg_id
                    """,
                    (run["account_run_id"], run["account_run_id"]),
                )
                identity.commit()
            finally:
                identity.execute("DETACH DATABASE source_identity")
        identity.execute(
            """
            UPDATE identity_provenance
               SET selected=1
             WHERE rowid IN (
               SELECT min(p.rowid)
                 FROM identity_provenance AS p
                 JOIN group_message_labels AS g
                   ON g.msg_id=p.published_msg_id AND g.label_id=p.label_id
                GROUP BY p.published_msg_id
             )
            """
        )
        source_labels = int(identity.execute("SELECT count(*) FROM identity_provenance").fetchone()[0])
        total_group = int(identity.execute("SELECT count(*) FROM msg.group_messages").fetchone()[0])
        labelled = int(identity.execute("SELECT count(*) FROM group_message_labels").fetchone()[0])
        cards = int(
            identity.execute(
                """SELECT count(*) FROM group_message_labels AS g JOIN label_values AS v
                     ON v.label_id=g.label_id WHERE v.label_source='historic_group_card'"""
            ).fetchone()[0]
        )
        nicknames = labelled - cards
        identity.execute(
            "INSERT INTO coverage VALUES (?,?,?,?,?,?,?,?)",
            (
                "group_message_historic_sender_labels", total_group, total_group,
                total_group, labelled, cards, nicknames, total_group - labelled,
            ),
        )
        identity.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("schema", IDENTITY_SCHEMA),
                ("account_id", display_id),
                ("publication_schema", PUBLICATION_SCHEMA),
                ("snapshot_present", "false"),
            ],
        )
        identity.commit()
        conflicts = int(
            identity.execute(
                """SELECT count(*) FROM (
                     SELECT published_msg_id FROM identity_provenance
                     GROUP BY published_msg_id HAVING count(DISTINCT label_id)>1)"""
            ).fetchone()[0]
        )
        result = {
            "source_identity_labels": source_labels,
            "published_identity_labels": labelled,
            "identity_label_values": int(identity.execute("SELECT count(*) FROM label_values").fetchone()[0]),
            "identity_conflict_messages": conflicts,
        }
    finally:
        identity.close()
    return result


def build_account(
    project: Path,
    warehouse: Path,
    publication_root: Path,
    account_key: str,
    display_id: str,
    publication_id: str,
    runs: list[sqlite3.Row],
    warehouse_objects: dict[str, str],
    object_sizes: dict[str, int],
) -> dict[str, Any]:
    account_root = publication_root / "accounts" / account_key
    structured = account_root / "structured"
    archive = account_root / "media_archive"
    structured.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    message_path = structured / "nt_msg_export.db"
    connection = sqlite3.connect(message_path, timeout=120, uri=True)
    connection.row_factory = sqlite3.Row
    init_message_database(connection)
    register_source_runs(connection, runs)
    observation_path = warehouse / "accounts" / account_key / "observations.sqlite"
    build_provenance(connection, observation_path, runs)
    copy_selected_messages(connection, project, runs)
    build_fts(connection)
    summary = message_summary(connection)
    connection.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [
            ("schema", MESSAGE_SCHEMA),
            ("publication_schema", PUBLICATION_SCHEMA),
            ("account_id", display_id),
            ("message_policy", "strict_one_to_one_cross_source_folds_only"),
            ("published_at", now()),
        ],
    )
    connection.commit()
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        stop("published message database quick_check failed")
    connection.execute("PRAGMA optimize")
    connection.close()

    media = build_media_view(
        project, warehouse, publication_root, archive, message_path, runs,
        warehouse_objects, object_sizes,
    )
    identity_path = archive / "index" / "identity_index.sqlite"
    identity = build_identity_view(project, identity_path, message_path, runs, display_id)
    message_bytes, message_sha = sha256_file(message_path)
    map_bytes, map_sha = sha256_file(archive / "index" / "media_map.sqlite")
    identity_bytes, identity_sha = sha256_file(identity_path)
    archive_manifest = {
        "schema": "qq-archive-viewer/1",
        "account_id": display_id,
        "export_run_id": publication_id,
        "message_store": {
            "path": "../structured/nt_msg_export.db",
            "format": "nt_msg_export/1",
            "tables": {"c2c": "c2c_messages", "group": "group_messages"},
            "canonical_provenance_tables": ["logical_messages", "message_provenance", "source_runs"],
        },
        "media_store": {
            "mapping_database": "index/media_map.sqlite",
            "media_root": "media",
            "format": MEDIA_SCHEMA,
            "object_policy": "publication_global_sha256_hardlinks",
        },
        "identity_store": {
            "path": "index/identity_index.sqlite",
            "format": IDENTITY_SCHEMA,
            "scope": "merged_historic_group_message_sender_labels",
            "provenance": "All source labels retained in identity_provenance; one deterministic label is rendered per logical message.",
        },
        "safety": {
            "network_access_required": False,
            "viewer_must_open_read_only": True,
            "all_paths_relative_to_manifest": True,
            "destructive_cleanup_authorized": False,
        },
        "canonical_publication": {
            "schema": PUBLICATION_SCHEMA,
            "message_observations": summary["observations"],
            "logical_messages": summary["logical_messages"],
            "strict_fold_groups": summary["strict_fold_groups"],
            "source_runs": len(runs),
        },
    }
    write_json(archive / "archive_manifest.json", archive_manifest)
    manifest_bytes, manifest_sha = sha256_file(archive / "archive_manifest.json")
    return {
        "account_key": account_key,
        "display_id": display_id,
        "archive_manifest": (archive / "archive_manifest.json").relative_to(publication_root).as_posix(),
        "source_runs": len(runs),
        "messages": summary,
        "media": media,
        "identity": identity,
        "artifacts": {
            "message_store": {"bytes": message_bytes, "sha256": message_sha},
            "media_map": {"bytes": map_bytes, "sha256": map_sha},
            "identity_index": {"bytes": identity_bytes, "sha256": identity_sha},
            "archive_manifest": {"bytes": manifest_bytes, "sha256": manifest_sha},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--warehouse-root", type=Path, required=True)
    parser.add_argument("--warehouse-verification", type=Path, required=True)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-deprecated-v1",
        action="store_true",
        help="explicitly acknowledge that v1 is frozen and deprecated",
    )
    args = parser.parse_args()

    if not args.allow_deprecated_v1:
        stop(
            "publication v1 is frozen and deprecated; use "
            "Build-QqCanonicalPublicationV2.py (or pass --allow-deprecated-v1 "
            "only for a documented legacy reproduction)"
        )

    project = args.project_root.resolve(strict=True)
    warehouse = args.warehouse_root.resolve(strict=True)
    verification_path = args.warehouse_verification.resolve(strict=True)
    verification = load_json(verification_path)
    if verification.get("schema") != "qq-canonical-warehouse-verification/1" or verification.get("status") != "ok":
        stop("warehouse verification is not successful")
    if verification.get("cas_missing_objects") or verification.get("hardlink_failures"):
        stop("warehouse verification has missing objects or hardlink failures")
    if any(char in args.publication_id for char in "\\/:*?\"<>|") or args.publication_id in {".", ".."}:
        stop("publication ID is not filesystem-safe")
    publications = warehouse / "publications"
    publications.mkdir(parents=True, exist_ok=True)
    target = publications / args.publication_id
    staging = publications / ("." + args.publication_id + ".staging")
    if target.exists() or staging.exists():
        stop("publication target or staging path already exists; immutable outputs are not overwritten")
    staging.mkdir()
    registry = sqlite3.connect(ro_uri(warehouse / "registry.sqlite"), uri=True)
    registry.row_factory = sqlite3.Row
    try:
        rows = list(
            registry.execute(
                """
                SELECT * FROM account_runs WHERE ingest_state='imported'
                ORDER BY account_key,account_run_id
                """
            )
        )
        warehouse_objects = {
            str(row["sha256"]): str(row["object_relative"])
            for row in registry.execute("SELECT sha256,object_relative FROM media_objects")
        }
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["account_key"]), []).append(row)
        if not grouped:
            stop("warehouse contains no imported account runs")
        object_sizes: dict[str, int] = {}
        accounts = []
        for ordinal, (account_key, account_runs) in enumerate(sorted(grouped.items()), start=1):
            account_runs.sort(
                key=lambda row: (SOURCE_PRIORITY.get(row["source_kind"], 99), row["account_run_id"])
            )
            accounts.append(
                build_account(
                    project, warehouse, staging, account_key, f"account_{ordinal:02d}",
                    args.publication_id,
                    account_runs, warehouse_objects, object_sizes,
                )
            )
        verification_bytes, verification_sha = sha256_file(verification_path)
        registry_bytes, registry_sha = sha256_file(warehouse / "registry.sqlite")
        manifest = {
            "schema": PUBLICATION_SCHEMA,
            "publication_id": args.publication_id,
            "status": "verified",
            "created_at": now(),
            "warehouse": {
                "schema": "qq-canonical-warehouse/1",
                "verification_bytes": verification_bytes,
                "verification_sha256": verification_sha,
                "registry_bytes": registry_bytes,
                "registry_sha256": registry_sha,
            },
            "message_policy": {
                "fold": "strict_one_to_one_cross_source_only",
                "ambiguous_or_unmatched": "preserve_as_distinct_logical_messages",
                "representative": "source_kind_priority_then_account_run_id",
                "provenance": "every imported observation retained",
            },
            "media_policy": {
                "content_identity": "sha256",
                "segment_selection": "strongest_verified_mapping_per_logical_message_segment",
                "candidate_names_never_promote_to_verified": True,
                "object_root": "objects/sha256",
                "materialization": "same_volume_hardlinks",
            },
            "accounts": accounts,
            "object_store": {
                "unique_objects": len(object_sizes),
                "logical_bytes": sum(object_sizes.values()),
                "physical_content_bytes_added": 0,
            },
            "safety": {
                "network_access_required": False,
                "source_rounds_modified": False,
                "destructive_cleanup_authorized": False,
                "contains_real_account_mapping": False,
            },
        }
        write_json(staging / "viewer_manifest.json", manifest)
        staging.replace(target)
    except BaseException:
        registry.close()
        raise
    else:
        registry.close()

    public_accounts = []
    for account in accounts:
        public_accounts.append(
            {
                "display_id": account["display_id"],
                "source_runs": account["source_runs"],
                "messages": account["messages"],
                "media": account["media"],
                "identity": account["identity"],
                "artifacts": account["artifacts"],
            }
        )
    report = {
        "schema": "qq-canonical-publication-build/1",
        "status": "ok",
        "publication_id": args.publication_id,
        "accounts": public_accounts,
        "object_store": {
            "unique_objects": len(object_sizes),
            "logical_bytes": sum(object_sizes.values()),
            "physical_content_bytes_added": 0,
        },
        "contains_account_key": False,
        "contains_chat_content": False,
    }
    if args.output:
        write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
