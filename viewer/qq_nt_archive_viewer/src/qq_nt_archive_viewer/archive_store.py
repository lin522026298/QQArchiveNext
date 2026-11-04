from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


PAGE_SIZE = 200
RAW_TABLE_FOR_STRUCTURED = {
    "c2c_messages": "c2c_msg_table",
    "group_messages": "group_msg_table",
}
CONVERSATION_CONFIGS = (
    ("c2c_messages", "私聊", "peer_qq", "peer_uid"),
    ("group_messages", "群聊", "group_qq", "group_id"),
)
IDENTITY_SCHEMAS = {"qq-identity-index/1", "qq-identity-index/2"}
HISTORIC_LABEL_SOURCES = {
    "historic_group_card",
    "historic_group_nickname",
}
SNAPSHOT_LABEL_SOURCES = {
    "snapshot_friend_remark",
    "snapshot_friend_nickname",
    "snapshot_group_remark",
    "snapshot_group_name",
    "snapshot_group_member_card",
    "snapshot_group_member_nickname",
}
ALL_IDENTITY_LABEL_SOURCES = HISTORIC_LABEL_SOURCES | SNAPSHOT_LABEL_SOURCES


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Conversation:
    source_table: str
    kind: str
    id_column: str
    conversation_id: str
    label: str
    message_count: int
    first_timestamp: int | None
    last_timestamp: int | None
    qq_column: str
    uid_column: str
    qq_value: int | None
    uid_values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Message:
    source_table: str
    msg_id: int
    timestamp: int | None
    direction: int | None
    sender_id: str
    sender_id_kind: str
    sender_label: str | None
    sender_label_source: str | None
    sent: bool
    msg_type: int | None
    text: str
    content: str


@dataclass(frozen=True, slots=True)
class MediaAsset:
    archive_relative: str
    detected_mime: str
    cache_class: str
    path: Path | None
    original_name: str | None = None


@dataclass(frozen=True, slots=True)
class MediaLink:
    source_table: str
    msg_id: int
    segment_index: int
    media_type: str
    role: str
    verification: str
    note: str
    filename: str | None
    primary: MediaAsset | None
    preview: MediaAsset | None
    playable: MediaAsset | None
    candidate: MediaAsset | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    conversation: Conversation
    msg_id: int
    timestamp: int | None
    sender_id: str
    sender_id_kind: str
    sender_label: str | None
    sender_label_source: str | None
    preview: str


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _nonzero_qq(value: Any) -> int | None:
    """Normalize QQ-number fields while treating 0 and blanks as unavailable."""

    if value in (None, "", 0, "0"):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result else None


def display_time(value: int | None) -> str:
    if not isinstance(value, int) or value <= 0:
        return "时间未知"
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        zone = dt.timezone(dt.timedelta(hours=8))
        return dt.datetime.fromtimestamp(seconds, zone).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "时间异常"


def _relative_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_summary(value: str, limit: int = 170) -> str:
    compact = value.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    compact = " ".join(compact.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def identity_source_title(source: str | None) -> str:
    return {
        "historic_group_card": "群名片",
        "historic_group_nickname": "昵称",
        "snapshot_friend_remark": "资料快照好友备注",
        "snapshot_friend_nickname": "资料快照昵称",
        "snapshot_group_remark": "资料快照群备注",
        "snapshot_group_name": "资料快照群名",
        "snapshot_group_member_card": "资料快照群名片",
        "snapshot_group_member_nickname": "资料快照群昵称",
    }.get(source or "", "历史名称")


def display_sender(
    sender_id: str,
    sender_id_kind: str,
    sender_label: str | None = None,
    sender_label_source: str | None = None,
    *,
    sent: bool = False,
) -> str:
    """Return a plain-text sender label without hiding its QQ/UID evidence.

    Identity sidecars carry historic, message-level group cards/nicknames.  A
    display label is therefore additive rather than a replacement for the
    primary sender identifier.  This avoids quietly treating a nickname as a
    private-contact remark or making a duplicate name ambiguous.
    """

    if sender_id:
        raw = f"{sender_id_kind} {sender_id}" if sender_id_kind else sender_id
    else:
        raw = "发送者未知"
    if sender_label:
        kind = identity_source_title(sender_label_source)
        identity = f"{kind}：{sender_label}"
        return f"我（{identity}） · {raw}" if sent else f"{identity} · {raw}"
    return f"我 · {raw}" if sent else raw


def extract_plain_content(text: str, content: str) -> str:
    """Render structured QQ content as plain text, never as HTML."""

    pieces: list[str] = []
    if text:
        pieces.append(text)
    try:
        parsed = json.loads(content) if content else None
    except (TypeError, json.JSONDecodeError):
        parsed = None

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        value_type = _text(value.get("type"))
        if value_type == "text" and value.get("text"):
            pieces.append(_text(value["text"]))
        elif value_type in {"image", "video", "file", "sticker"}:
            label = {
                "image": "[图片]",
                "video": "[视频]",
                "file": "[文件]",
                "sticker": "[表情]",
            }[value_type]
            pieces.append(label)
        for key in ("segments", "items"):
            nested = value.get(key)
            if isinstance(nested, list):
                visit(nested)

    visit(parsed)
    seen: set[str] = set()
    result: list[str] = []
    for item in pieces:
        item = item.replace("\x00", " ")
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return "\n".join(result) if result else "[非文本 QQ 消息]"


class ArchiveStore:
    """Read-only view over one structured QQ NT export and its media map."""

    def __init__(
        self, manifest_path: Path, account_label: str | None = None,
        *, deep_validation: bool = False, cache_dir: Path | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.deep_validation = deep_validation
        self.cache_dir = cache_dir
        self.manifest_path = manifest_path.resolve(strict=True)
        if self.manifest_path.name != "archive_manifest.json":
            raise ArchiveError("请选择 media_archive 目录中的 archive_manifest.json")
        self.archive_dir = self.manifest_path.parent
        self.export_dir = self.archive_dir.parent
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"无法读取归档清单：{exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") not in {
            "qq-archive-viewer/1",
            "qq-archive-viewer/2",
        }:
            raise ArchiveError("不支持的归档清单格式")
        self.manifest = manifest
        self.archive_schema = _text(manifest.get("schema"))
        self.storage_account_id = _text(
            manifest.get("account_id") or manifest.get("storage_account_id")
        ) or "未知账号"
        self.account_id = _text(account_label) or self.storage_account_id
        self.publication_root: Path | None = None
        if self.archive_schema == "qq-archive-viewer/2":
            publication = manifest.get("publication")
            if not isinstance(publication, dict):
                raise ArchiveError("v2 账号归档缺少统一发布根声明")
            root_value = publication.get("root")
            if not isinstance(root_value, str) or not root_value or Path(root_value).is_absolute():
                raise ArchiveError("v2 统一发布根路径无效")
            self.publication_root = (self.archive_dir / Path(root_value)).resolve(strict=True)
            if not self.publication_root.is_dir():
                raise ArchiveError("v2 统一发布根目录不存在")
        message_path = self._resolve_export_file(
            manifest.get("message_store", {}).get("path"), "消息数据库"
        )
        map_path = self._resolve_archive_file(
            manifest.get("media_store", {}).get("mapping_database"), "媒体映射数据库"
        )
        if self.archive_schema == "qq-archive-viewer/2":
            self.media_root = self._resolve_publication_directory(
                manifest.get("media_store", {}).get("object_root"), "全局 CAS"
            )
        else:
            self.media_root = self._resolve_archive_directory(
                manifest.get("media_store", {}).get("media_root"), "媒体目录"
            )
        self.message_connection = _read_only_connection(message_path)
        self.map_connection = _read_only_connection(map_path)
        self.identity_connection: sqlite3.Connection | None = None
        self.identity_coverage: dict[str, int] | None = None
        self.identity_snapshot_coverage: dict[str, dict[str, int]] | None = None
        self.identity_snapshot_id: str | None = None
        self.identity_schema: str | None = None
        self.identity_declared_schema: str | None = None
        self._snapshot_contact_cache: dict[tuple[str, str], tuple[str, str] | None] = {}
        self._snapshot_group_title_cache: dict[str, tuple[str, str] | None] = {}
        self._snapshot_member_cache: dict[
            tuple[str, str, str], tuple[str, str] | None
        ] = {}
        identity_store = manifest.get("identity_store")
        if identity_store is not None:
            if not isinstance(identity_store, dict):
                raise ArchiveError("身份侧车库声明无效")
            if identity_store.get("format") not in IDENTITY_SCHEMAS:
                raise ArchiveError("不支持的身份侧车库格式")
            self.identity_declared_schema = _text(identity_store.get("format"))
            identity_path = self._resolve_archive_file(
                identity_store.get("path"), "身份侧车库"
            )
            self.identity_connection = _read_only_connection(identity_path)
        if cancelled:
            for connection in (self.message_connection, self.map_connection, self.identity_connection):
                if connection is not None:
                    connection.set_progress_handler(lambda: int(cancelled()), 10000)
        try:
            self._validate_schema()
            if deep_validation and self.identity_connection is None:
                self.verify_integrity()
        except BaseException:
            self.close()
            raise
        self._conversation_cache: list[Conversation] | None = None
        self._conversation_index: dict[tuple[str, str, str], Conversation] | None = None

    def verify_integrity(self) -> None:
        for connection in (self.message_connection, self.map_connection, self.identity_connection):
            if connection is not None:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ArchiveError("归档完整性检查失败")
                if list(connection.execute("PRAGMA foreign_key_check")):
                    raise ArchiveError("归档存在外键引用错误")

    def _resolve_export_file(self, relative: Any, label: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise ArchiveError(f"归档清单缺少{label}路径")
        candidate = (self.archive_dir / Path(relative)).resolve(strict=True)
        if not _relative_inside(candidate, self.export_dir):
            raise ArchiveError(f"{label}路径越出导出运行目录")
        if not candidate.is_file():
            raise ArchiveError(f"{label}不存在：{candidate}")
        return candidate

    def _resolve_archive_file(self, relative: Any, label: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise ArchiveError(f"归档清单缺少{label}路径")
        candidate = (self.archive_dir / Path(relative)).resolve(strict=True)
        if not _relative_inside(candidate, self.archive_dir):
            raise ArchiveError(f"{label}路径越出媒体归档目录")
        if not candidate.is_file():
            raise ArchiveError(f"{label}不存在：{candidate}")
        return candidate

    def _resolve_archive_directory(self, relative: Any, label: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise ArchiveError(f"归档清单缺少{label}路径")
        candidate = (self.archive_dir / Path(relative)).resolve(strict=True)
        if not _relative_inside(candidate, self.archive_dir):
            raise ArchiveError(f"{label}路径越出媒体归档目录")
        if not candidate.is_dir():
            raise ArchiveError(f"{label}不存在：{candidate}")
        return candidate

    def _resolve_publication_directory(self, relative: Any, label: str) -> Path:
        if self.publication_root is None:
            raise ArchiveError("归档没有统一发布根目录")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ArchiveError(f"归档清单缺少{label}路径")
        candidate = (self.archive_dir / Path(relative)).resolve(strict=True)
        if not _relative_inside(candidate, self.publication_root):
            raise ArchiveError(f"{label}路径越出统一发布目录")
        if not candidate.is_dir():
            raise ArchiveError(f"{label}不存在：{candidate}")
        return candidate

    def _validate_schema(self) -> None:
        if self.manifest.get("message_store", {}).get("format") != "nt_msg_export/1":
            raise ArchiveError("不支持的结构化消息库格式")
        message_tables = {
            row["name"]
            for row in self.message_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_messages = {"c2c_messages", "group_messages"} - message_tables
        if missing_messages:
            raise ArchiveError(
                "消息数据库缺少表：" + "、".join(sorted(missing_messages))
            )
        map_tables = {
            row["name"]
            for row in self.map_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_map = {"metadata", "assets", "message_media"} - map_tables
        if missing_map:
            raise ArchiveError(
                "媒体映射数据库缺少表：" + "、".join(sorted(missing_map))
            )
        expected_message_columns = {
            "c2c_messages": {
                "msg_id", "timestamp", "direction", "sender_uid", "sender_qq",
                "peer_uid", "peer_qq", "msg_type", "text", "content",
            },
            "group_messages": {
                "msg_id", "timestamp", "direction", "sender_uid", "sender_qq",
                "group_id", "group_qq", "msg_type", "text", "content",
            },
        }
        for table, expected in expected_message_columns.items():
            columns = {
                row["name"]
                for row in self.message_connection.execute(f"PRAGMA table_info({table})")
            }
            missing = expected - columns
            if missing:
                raise ArchiveError(
                    f"消息表 {table} 缺少字段：" + "、".join(sorted(missing))
                )
        expected_map_columns = {
            "assets": {"asset_id", "archive_relative", "detected_mime", "cache_class"},
            "message_media": {
                "link_id", "source_table", "msg_id", "segment_index", "media_type",
                "role", "verification", "note", "primary_asset_id", "preview_asset_id",
                "playable_asset_id", "candidate_asset_id",
            },
        }
        for table, expected in expected_map_columns.items():
            columns = {
                row["name"]
                for row in self.map_connection.execute(f"PRAGMA table_info({table})")
            }
            missing = expected - columns
            if missing:
                raise ArchiveError(
                    f"媒体映射表 {table} 缺少字段：" + "、".join(sorted(missing))
                )
        metadata = {
            row["key"]: row["value"]
            for row in self.map_connection.execute("SELECT key, value FROM metadata")
        }
        try:
            map_schema = json.loads(metadata.get("schema", '""'))
        except json.JSONDecodeError as exc:
            raise ArchiveError("媒体映射数据库 schema 元数据无效") from exc
        if map_schema not in {"qq-media-archive/1", "qq-media-map/2"}:
            raise ArchiveError("不支持的媒体映射数据库格式")
        self._validate_identity_schema()

    def _validate_identity_schema(self) -> None:
        """Validate the optional, derived message-level identity sidecar."""

        if self.identity_connection is None:
            return
        tables = {
            row["name"]
            for row in self.identity_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = {"metadata", "coverage", "label_values", "group_message_labels"} - tables
        if missing:
            raise ArchiveError("身份侧车库缺少表：" + "、".join(sorted(missing)))
        expected_columns = {
            "metadata": {"key", "value"},
            "coverage": {
                "scope", "total_structured_messages", "raw_messages",
                "joined_messages", "labelled_messages", "group_card_messages",
                "nickname_fallback_messages", "unlabelled_messages",
            },
            "label_values": {
                "label_id", "display_name", "label_source",
            },
            "group_message_labels": {
                "msg_id", "label_id",
            },
        }
        for table, expected in expected_columns.items():
            columns = {
                row["name"]
                for row in self.identity_connection.execute(f"PRAGMA table_info({table})")
            }
            missing_columns = expected - columns
            if missing_columns:
                raise ArchiveError(
                    f"身份侧车库表 {table} 缺少字段："
                    + "、".join(sorted(missing_columns))
                )
        metadata = {
            row["key"]: row["value"]
            for row in self.identity_connection.execute("SELECT key, value FROM metadata")
        }
        schema = _text(metadata.get("schema"))
        if schema not in IDENTITY_SCHEMAS:
            raise ArchiveError("身份侧车库 schema 元数据无效")
        if schema != self.identity_declared_schema:
            raise ArchiveError("身份侧车库 schema 与归档清单不一致")
        self.identity_schema = schema
        if _text(metadata.get("account_id")) != self.storage_account_id:
            raise ArchiveError("身份侧车库账号与归档清单不一致")
        if self.deep_validation:
            for connection in (self.message_connection, self.map_connection, self.identity_connection):
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ArchiveError(f"归档完整性检查失败：{integrity}")
                if list(connection.execute("PRAGMA foreign_key_check")):
                    raise ArchiveError("归档存在外键引用错误")
        coverage = self.identity_connection.execute(
            """
            SELECT total_structured_messages, raw_messages, joined_messages,
                   labelled_messages, group_card_messages,
                   nickname_fallback_messages, unlabelled_messages
              FROM coverage
             WHERE scope = 'group_message_historic_sender_labels'
            """
        ).fetchone()
        if coverage is None:
            raise ArchiveError("身份侧车库缺少群消息覆盖率记录")
        values = {key: int(coverage[key]) for key in coverage.keys()}
        if values["labelled_messages"] != (
            values["group_card_messages"] + values["nickname_fallback_messages"]
        ):
            raise ArchiveError("身份侧车库覆盖率计数不一致")
        if values["unlabelled_messages"] != (
            values["total_structured_messages"] - values["labelled_messages"]
        ):
            raise ArchiveError("身份侧车库未标注计数不一致")
        self.identity_coverage = values
        if not self.deep_validation:
            if schema != "qq-identity-index/1":
                self.identity_snapshot_id = _text(metadata.get("identity_snapshot_id")) or None
            return
        label_count = self.identity_connection.execute(
            "SELECT count(*) FROM group_message_labels"
        ).fetchone()[0]
        if int(label_count) != values["labelled_messages"]:
            raise ArchiveError("身份侧车库标签数与覆盖率不一致")
        invalid_values = self.identity_connection.execute(
            """
            SELECT count(*)
              FROM label_values
             WHERE trim(replace(display_name, char(0), ' ')) = ''
                OR label_source NOT IN (
                    'historic_group_card', 'historic_group_nickname',
                    'snapshot_friend_remark', 'snapshot_friend_nickname',
                    'snapshot_group_remark', 'snapshot_group_name',
                    'snapshot_group_member_card', 'snapshot_group_member_nickname'
                )
            """
        ).fetchone()[0]
        if int(invalid_values):
            raise ArchiveError("身份侧车库包含无效名称或来源")
        invalid_historic = self.identity_connection.execute(
            """
            SELECT count(*)
              FROM group_message_labels AS m
              JOIN label_values AS v ON v.label_id = m.label_id
             WHERE v.label_source NOT IN ('historic_group_card', 'historic_group_nickname')
            """
        ).fetchone()[0]
        if int(invalid_historic):
            raise ArchiveError("历史消息标签包含不允许的来源")
        self.identity_coverage = values
        if schema == "qq-identity-index/1":
            return

        snapshot_tables = {
            "snapshot_coverage",
            "snapshot_c2c_lookups",
            "snapshot_group_titles",
            "snapshot_group_member_lookups",
        }
        missing_snapshot = snapshot_tables - tables
        if missing_snapshot:
            raise ArchiveError(
                "身份资料快照侧车库缺少表：" + "、".join(sorted(missing_snapshot))
            )
        snapshot_columns = {
            "snapshot_coverage": {
                "scope", "source_rows", "labelled_source_rows",
                "primary_lookup_keys", "fallback_lookup_keys",
                "ambiguous_lookup_keys", "unlabelled_source_rows",
            },
            "snapshot_c2c_lookups": {"lookup_kind", "lookup_value", "label_id"},
            "snapshot_group_titles": {"group_number", "label_id"},
            "snapshot_group_member_lookups": {
                "group_number", "lookup_kind", "lookup_value", "label_id",
            },
        }
        for table, expected in snapshot_columns.items():
            columns = {
                row["name"]
                for row in self.identity_connection.execute(f"PRAGMA table_info({table})")
            }
            missing_columns = expected - columns
            if missing_columns:
                raise ArchiveError(
                    f"身份资料快照表 {table} 缺少字段："
                    + "、".join(sorted(missing_columns))
                )
        snapshot_present = _text(metadata.get("snapshot_present"))
        if snapshot_present not in {"true", "false"}:
            raise ArchiveError("身份资料快照存在标记无效")
        self.identity_snapshot_id = (
            _text(metadata.get("identity_snapshot_id")) or None
            if snapshot_present == "true"
            else None
        )
        if snapshot_present == "true" and self.identity_snapshot_id is None:
            raise ArchiveError("身份资料快照缺少来源标识")
        rows = list(
            self.identity_connection.execute(
                """
                SELECT scope, source_rows, labelled_source_rows,
                       primary_lookup_keys, fallback_lookup_keys,
                       ambiguous_lookup_keys, unlabelled_source_rows
                  FROM snapshot_coverage
                 ORDER BY scope
                """
            )
        )
        if snapshot_present == "true" and {
            _text(row["scope"]) for row in rows
        } != {"profile_contact_labels", "group_title_labels", "group_member_labels"}:
            raise ArchiveError("身份资料快照覆盖率范围不完整")
        if snapshot_present == "false" and rows:
            raise ArchiveError("未声明资料快照却含有快照覆盖率")
        coverage_rows: dict[str, dict[str, int]] = {}
        for row in rows:
            values_for_row = {
                key: int(row[key])
                for key in (
                    "source_rows", "labelled_source_rows", "primary_lookup_keys",
                    "fallback_lookup_keys", "ambiguous_lookup_keys",
                    "unlabelled_source_rows",
                )
            }
            if (
                values_for_row["labelled_source_rows"]
                + values_for_row["unlabelled_source_rows"]
                != values_for_row["source_rows"]
            ):
                raise ArchiveError("身份资料快照覆盖率计数不一致")
            if any(value < 0 for value in values_for_row.values()):
                raise ArchiveError("身份资料快照覆盖率包含负数")
            coverage_rows[_text(row["scope"])] = values_for_row
        lookup_counts = {
            "profile_contact_labels": (
                int(
                    self.identity_connection.execute(
                        "SELECT count(*) FROM snapshot_c2c_lookups WHERE lookup_kind = 'uid'"
                    ).fetchone()[0]
                ),
                int(
                    self.identity_connection.execute(
                        "SELECT count(*) FROM snapshot_c2c_lookups WHERE lookup_kind = 'qq'"
                    ).fetchone()[0]
                ),
            ),
            "group_title_labels": (
                int(
                    self.identity_connection.execute(
                        "SELECT count(*) FROM snapshot_group_titles"
                    ).fetchone()[0]
                ),
                0,
            ),
            "group_member_labels": (
                int(
                    self.identity_connection.execute(
                        "SELECT count(*) FROM snapshot_group_member_lookups WHERE lookup_kind = 'uid'"
                    ).fetchone()[0]
                ),
                int(
                    self.identity_connection.execute(
                        "SELECT count(*) FROM snapshot_group_member_lookups WHERE lookup_kind = 'qq'"
                    ).fetchone()[0]
                ),
            ),
        }
        if snapshot_present == "false" and any(
            primary or fallback for primary, fallback in lookup_counts.values()
        ):
            raise ArchiveError("未声明资料快照却含有快照映射")
        for scope, (primary, fallback) in lookup_counts.items():
            if scope in coverage_rows and (
                coverage_rows[scope]["primary_lookup_keys"] != primary
                or coverage_rows[scope]["fallback_lookup_keys"] != fallback
            ):
                raise ArchiveError("身份资料快照映射数量与覆盖率不一致")
        invalid_snapshot_keys = self.identity_connection.execute(
            """
            SELECT count(*) FROM snapshot_c2c_lookups
             WHERE lookup_kind NOT IN ('uid', 'qq')
                OR trim(replace(lookup_value, char(0), ' ')) = ''
            UNION ALL
            SELECT count(*) FROM snapshot_group_titles
             WHERE trim(replace(group_number, char(0), ' ')) = ''
            UNION ALL
            SELECT count(*) FROM snapshot_group_member_lookups
             WHERE lookup_kind NOT IN ('uid', 'qq')
                OR trim(replace(group_number, char(0), ' ')) = ''
                OR trim(replace(lookup_value, char(0), ' ')) = ''
            """
        ).fetchall()
        if sum(int(row[0]) for row in invalid_snapshot_keys):
            raise ArchiveError("身份资料快照包含无效查找键")
        source_checks = (
            (
                "snapshot_c2c_lookups",
                "'snapshot_friend_remark', 'snapshot_friend_nickname'",
            ),
            (
                "snapshot_group_titles",
                "'snapshot_group_remark', 'snapshot_group_name'",
            ),
            (
                "snapshot_group_member_lookups",
                "'snapshot_group_member_card', 'snapshot_group_member_nickname'",
            ),
        )
        for table, allowed_sources in source_checks:
            invalid_source = self.identity_connection.execute(
                f"""
                SELECT count(*)
                  FROM {table} AS m
                  JOIN label_values AS v ON v.label_id = m.label_id
                 WHERE v.label_source NOT IN ({allowed_sources})
                """
            ).fetchone()[0]
            if int(invalid_source):
                raise ArchiveError("身份资料快照映射的名称来源不匹配")
        self.identity_snapshot_coverage = coverage_rows or None

    def close(self) -> None:
        self.message_connection.close()
        self.map_connection.close()
        if self.identity_connection is not None:
            self.identity_connection.close()

    def overview(self) -> dict[str, Any]:
        count_c2c = self.message_connection.execute(
            "SELECT count(*) FROM c2c_messages"
        ).fetchone()[0]
        count_group = self.message_connection.execute(
            "SELECT count(*) FROM group_messages"
        ).fetchone()[0]
        map_count = self.map_connection.execute(
            "SELECT count(*) FROM message_media"
        ).fetchone()[0]
        return {
            "account_id": self.account_id,
            "c2c_messages": int(count_c2c),
            "group_messages": int(count_group),
            "media_links": int(map_count),
            "identity_labels": (
                self.identity_coverage["labelled_messages"]
                if self.identity_coverage is not None
                else 0
            ),
            "identity_coverage": self.identity_coverage,
            "identity_snapshot_coverage": self.identity_snapshot_coverage,
            "identity_snapshot_id": self.identity_snapshot_id,
            "manifest": str(self.manifest_path),
        }

    def _snapshot_lookup_contact(
        self, uid_values: Iterable[str], qq_value: int | None
    ) -> tuple[str, str] | None:
        """Resolve profile UID first, then QQ; reject conflicting UID labels."""

        if self.identity_connection is None or self.identity_schema != "qq-identity-index/2":
            return None
        uid_matches: set[tuple[str, str]] = set()
        for uid_value in sorted({_text(value) for value in uid_values if _text(value)}):
            cache_key = ("uid", uid_value)
            if cache_key not in self._snapshot_contact_cache:
                row = self.identity_connection.execute(
                    """
                    SELECT v.display_name, v.label_source
                      FROM snapshot_c2c_lookups AS m
                      JOIN label_values AS v ON v.label_id = m.label_id
                     WHERE m.lookup_kind = 'uid' AND m.lookup_value = ?
                    """,
                    (uid_value,),
                ).fetchone()
                self._snapshot_contact_cache[cache_key] = (
                    (_text(row["display_name"]), _text(row["label_source"]))
                    if row is not None
                    else None
                )
            match = self._snapshot_contact_cache[cache_key]
            if match is not None:
                uid_matches.add(match)
        if len(uid_matches) == 1:
            return next(iter(uid_matches))
        if len(uid_matches) > 1:
            return None
        if qq_value is None:
            return None
        qq_text = str(qq_value)
        cache_key = ("qq", qq_text)
        if cache_key not in self._snapshot_contact_cache:
            row = self.identity_connection.execute(
                """
                SELECT v.display_name, v.label_source
                  FROM snapshot_c2c_lookups AS m
                  JOIN label_values AS v ON v.label_id = m.label_id
                 WHERE m.lookup_kind = 'qq' AND m.lookup_value = ?
                """,
                (qq_text,),
            ).fetchone()
            self._snapshot_contact_cache[cache_key] = (
                (_text(row["display_name"]), _text(row["label_source"]))
                if row is not None
                else None
            )
        return self._snapshot_contact_cache[cache_key]

    @staticmethod
    def _group_number(qq_value: int | None, uid_values: Iterable[str]) -> str | None:
        if qq_value is not None:
            return str(qq_value)
        candidates = {
            str(candidate)
            for value in uid_values
            if (candidate := _nonzero_qq(value)) is not None
        }
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _snapshot_lookup_group_title(
        self, qq_value: int | None, uid_values: Iterable[str]
    ) -> tuple[str, str] | None:
        if self.identity_connection is None or self.identity_schema != "qq-identity-index/2":
            return None
        group_number = self._group_number(qq_value, uid_values)
        if group_number is None:
            return None
        if group_number not in self._snapshot_group_title_cache:
            row = self.identity_connection.execute(
                """
                SELECT v.display_name, v.label_source
                  FROM snapshot_group_titles AS t
                  JOIN label_values AS v ON v.label_id = t.label_id
                 WHERE t.group_number = ?
                """,
                (group_number,),
            ).fetchone()
            self._snapshot_group_title_cache[group_number] = (
                (_text(row["display_name"]), _text(row["label_source"]))
                if row is not None
                else None
            )
        return self._snapshot_group_title_cache[group_number]

    def _snapshot_lookup_group_member(
        self,
        group_qq: Any,
        group_id: Any,
        sender_uid: Any,
        sender_qq: Any,
    ) -> tuple[str, str] | None:
        """Resolve group+UID first, then group+QQ; never cross group borders."""

        if self.identity_connection is None or self.identity_schema != "qq-identity-index/2":
            return None
        group_number = self._group_number(_nonzero_qq(group_qq), (_text(group_id),))
        if group_number is None:
            return None
        uid = _text(sender_uid)
        if uid:
            cache_key = (group_number, "uid", uid)
            if cache_key not in self._snapshot_member_cache:
                row = self.identity_connection.execute(
                    """
                    SELECT v.display_name, v.label_source
                      FROM snapshot_group_member_lookups AS m
                      JOIN label_values AS v ON v.label_id = m.label_id
                     WHERE m.group_number = ? AND m.lookup_kind = 'uid'
                       AND m.lookup_value = ?
                    """,
                    (group_number, uid),
                ).fetchone()
                self._snapshot_member_cache[cache_key] = (
                    (_text(row["display_name"]), _text(row["label_source"]))
                    if row is not None
                    else None
                )
            match = self._snapshot_member_cache[cache_key]
            if match is not None:
                return match
        qq = _nonzero_qq(sender_qq)
        if qq is None:
            return None
        cache_key = (group_number, "qq", str(qq))
        if cache_key not in self._snapshot_member_cache:
            row = self.identity_connection.execute(
                """
                SELECT v.display_name, v.label_source
                  FROM snapshot_group_member_lookups AS m
                  JOIN label_values AS v ON v.label_id = m.label_id
                 WHERE m.group_number = ? AND m.lookup_kind = 'qq'
                   AND m.lookup_value = ?
                """,
                (group_number, str(qq)),
            ).fetchone()
            self._snapshot_member_cache[cache_key] = (
                (_text(row["display_name"]), _text(row["label_source"]))
                if row is not None
                else None
            )
        return self._snapshot_member_cache[cache_key]

    @staticmethod
    def _conversation_raw_identity(
        qq_value: int | None, uid_values: Iterable[str]
    ) -> str:
        parts: list[str] = [f"QQ {qq_value}"] if qq_value is not None else []
        unique_uids = sorted({_text(value) for value in uid_values if _text(value)})
        if unique_uids:
            visible = unique_uids[:3]
            parts.extend(f"UID {value}" for value in visible)
            if len(unique_uids) > len(visible):
                parts.append(f"另有 {len(unique_uids) - len(visible)} 个 UID")
        return " · ".join(parts) if parts else "身份未知"

    def _conversation_label(
        self, kind: str, qq_value: int | None, uid_values: Iterable[str]
    ) -> str:
        raw = self._conversation_raw_identity(qq_value, uid_values)
        snapshot: tuple[str, str] | None
        if kind == "私聊":
            snapshot = self._snapshot_lookup_contact(uid_values, qq_value)
        else:
            snapshot = self._snapshot_lookup_group_title(qq_value, uid_values)
        if snapshot is None:
            return f"{kind} · {raw}"
        return f"{kind} · {identity_source_title(snapshot[1])}：{snapshot[0]} · {raw}"

    def _load_conversations(self) -> list[Conversation]:
        if self._conversation_cache is not None:
            return self._conversation_cache
        from . import summary_cache
        cache_key = None
        if self.cache_dir is not None:
            cache_key = summary_cache.binding(self.manifest_path, (self.message_connection, self.identity_connection))
            cached = summary_cache.load(self.cache_dir, cache_key, Conversation)
            if cached is not None:
                self._set_conversations(cached)
                return cached
        conversations: list[Conversation] = []
        index: dict[tuple[str, str, str], Conversation] = {}

        def stat_from_row(row: sqlite3.Row) -> tuple[int, int | None, int | None]:
            return (
                int(row["message_count"]),
                int(row["first_timestamp"])
                if row["first_timestamp"] is not None
                else None,
                int(row["last_timestamp"])
                if row["last_timestamp"] is not None
                else None,
            )

        def add_stats(
            left: tuple[int, int | None, int | None],
            right: tuple[int, int | None, int | None],
        ) -> tuple[int, int | None, int | None]:
            firsts = [value for value in (left[1], right[1]) if value is not None]
            lasts = [value for value in (left[2], right[2]) if value is not None]
            return (
                left[0] + right[0],
                min(firsts) if firsts else None,
                max(lasts) if lasts else None,
            )

        for source_table, kind, qq_column, uid_column in CONVERSATION_CONFIGS:
            # QQ NT can store one history under a numeric QQ number and another
            # under its UID.  Only merge aliases that unambiguously identify one
            # QQ number; uncertain UIDs remain a separately labelled timeline.
            uid_to_qqs: dict[str, set[int]] = defaultdict(set)
            for row in self.message_connection.execute(
                f"""
                SELECT {qq_column} AS qq_value, {uid_column} AS uid_value
                  FROM {source_table}
                 WHERE {qq_column} IS NOT NULL AND {qq_column} != 0
                   AND {uid_column} IS NOT NULL AND {uid_column} != ''
                 GROUP BY {qq_column}, {uid_column}
                """
            ):
                qq_value = _nonzero_qq(row["qq_value"])
                uid_value = _text(row["uid_value"])
                if qq_value is not None and uid_value:
                    uid_to_qqs[uid_value].add(qq_value)
            uid_to_qq = {
                uid_value: next(iter(qq_values))
                for uid_value, qq_values in uid_to_qqs.items()
                if len(qq_values) == 1
            }
            qq_to_uids: dict[int, set[str]] = defaultdict(set)
            for uid_value, qq_value in uid_to_qq.items():
                qq_to_uids[qq_value].add(uid_value)

            qq_stats = {
                _nonzero_qq(row["qq_value"]): stat_from_row(row)
                for row in self.message_connection.execute(
                    f"""
                    SELECT {qq_column} AS qq_value, count(*) AS message_count,
                           min(timestamp) AS first_timestamp,
                           max(timestamp) AS last_timestamp
                      FROM {source_table}
                     WHERE {qq_column} IS NOT NULL AND {qq_column} != 0
                     GROUP BY {qq_column}
                    """
                )
                if _nonzero_qq(row["qq_value"]) is not None
            }
            uid_stats = {
                _text(row["uid_value"]): stat_from_row(row)
                for row in self.message_connection.execute(
                    f"""
                    SELECT {uid_column} AS uid_value, count(*) AS message_count,
                           min(timestamp) AS first_timestamp,
                           max(timestamp) AS last_timestamp
                      FROM {source_table}
                     WHERE ({qq_column} IS NULL OR {qq_column} = 0)
                       AND {uid_column} IS NOT NULL AND {uid_column} != ''
                     GROUP BY {uid_column}
                    """
                )
            }

            for qq_value, stats in qq_stats.items():
                assert qq_value is not None
                aliases = tuple(sorted(qq_to_uids.get(qq_value, ())))
                merged_stats = stats
                for uid_value in aliases:
                    if uid_value in uid_stats:
                        merged_stats = add_stats(merged_stats, uid_stats[uid_value])
                conversation = Conversation(
                    source_table=source_table,
                    kind=kind,
                    id_column=qq_column,
                    conversation_id=str(qq_value),
                    label=self._conversation_label(kind, qq_value, aliases),
                    message_count=merged_stats[0],
                    first_timestamp=merged_stats[1],
                    last_timestamp=merged_stats[2],
                    qq_column=qq_column,
                    uid_column=uid_column,
                    qq_value=qq_value,
                    uid_values=aliases,
                )
                conversations.append(conversation)
                index[(source_table, "qq", str(qq_value))] = conversation
                for uid_value in aliases:
                    index[(source_table, "uid", uid_value)] = conversation

            for uid_value, stats in uid_stats.items():
                if uid_value in uid_to_qq:
                    continue
                conversation = Conversation(
                    source_table=source_table,
                    kind=kind,
                    id_column=uid_column,
                    conversation_id=uid_value,
                    label=self._conversation_label(kind, None, (uid_value,)),
                    message_count=stats[0],
                    first_timestamp=stats[1],
                    last_timestamp=stats[2],
                    qq_column=qq_column,
                    uid_column=uid_column,
                    qq_value=None,
                    uid_values=(uid_value,),
                )
                conversations.append(conversation)
                index[(source_table, "uid", uid_value)] = conversation
        conversations.sort(
            key=lambda item: (
                item.last_timestamp is not None,
                item.last_timestamp or 0,
                item.label,
            ),
            reverse=True,
        )
        self._conversation_cache = conversations
        self._conversation_index = index
        if self.cache_dir is not None and cache_key == summary_cache.binding(
            self.manifest_path, (self.message_connection, self.identity_connection)
        ):
            try:
                summary_cache.save(self.cache_dir, cache_key, conversations)
            except (OSError, ValueError):
                pass  # A disposable cache cannot prevent read-only browsing.
        return conversations

    def _set_conversations(self, values: list[Conversation]) -> None:
        self._conversation_cache = values
        self._conversation_index = {}
        for item in values:
            if item.qq_value is not None:
                self._conversation_index[(item.source_table, "qq", str(item.qq_value))] = item
            for uid in item.uid_values:
                self._conversation_index[(item.source_table, "uid", uid)] = item

    def conversations(self, query: str = "", kind: str = "全部") -> list[Conversation]:
        normalized = query.strip().casefold()
        values = self._load_conversations()
        result = [
            item
            for item in values
            if (kind == "全部" or item.kind == kind)
            and (
                not normalized
                or normalized in item.label.casefold()
                or normalized in item.conversation_id.casefold()
            )
        ]
        return result

    @staticmethod
    def _conversation_clause(
        conversation: Conversation, prefix: str = ""
    ) -> tuple[str, list[Any]]:
        """Build the exact QQ/UID union represented by one visible timeline."""

        qq_column = prefix + conversation.qq_column
        uid_column = prefix + conversation.uid_column
        if conversation.qq_value is not None:
            clauses = [f"{qq_column} = ?"]
            parameters: list[Any] = [conversation.qq_value]
            if conversation.uid_values:
                placeholders = ",".join("?" for _ in conversation.uid_values)
                clauses.append(
                    f"(({qq_column} IS NULL OR {qq_column} = 0) "
                    f"AND {uid_column} IN ({placeholders}))"
                )
                parameters.extend(conversation.uid_values)
            return "(" + " OR ".join(clauses) + ")", parameters
        if not conversation.uid_values:
            raise ArchiveError("UID 会话缺少查询身份")
        return (
            f"({uid_column} = ? AND ({qq_column} IS NULL OR {qq_column} = 0))",
            [conversation.uid_values[0]],
        )

    def _conversation_for_identity(
        self,
        source_table: str,
        kind: str,
        qq_column: str,
        uid_column: str,
        qq_value: Any,
        uid_value: Any,
    ) -> Conversation:
        self._load_conversations()
        assert self._conversation_index is not None
        normalized_qq = _nonzero_qq(qq_value)
        normalized_uid = _text(uid_value)
        if normalized_qq is not None:
            known = self._conversation_index.get(
                (source_table, "qq", str(normalized_qq))
            )
            if known:
                return known
        if normalized_uid:
            known = self._conversation_index.get((source_table, "uid", normalized_uid))
            if known:
                return known
        # A search row normally always resolves above.  Keep a safe fallback for
        # a database that changes while it is open instead of fabricating SQL.
        if normalized_qq is not None:
            return Conversation(
                source_table=source_table,
                kind=kind,
                id_column=qq_column,
                conversation_id=str(normalized_qq),
                label=self._conversation_label(kind, normalized_qq, ()),
                message_count=0,
                first_timestamp=None,
                last_timestamp=None,
                qq_column=qq_column,
                uid_column=uid_column,
                qq_value=normalized_qq,
                uid_values=(),
            )
        if normalized_uid:
            return Conversation(
                source_table=source_table,
                kind=kind,
                id_column=uid_column,
                conversation_id=normalized_uid,
                label=self._conversation_label(kind, None, (normalized_uid,)),
                message_count=0,
                first_timestamp=None,
                last_timestamp=None,
                qq_column=qq_column,
                uid_column=uid_column,
                qq_value=None,
                uid_values=(normalized_uid,),
            )
        raise ArchiveError("搜索结果缺少会话身份")

    def _message_query(
        self,
        conversation: Conversation,
        *,
        before: tuple[int, int] | None = None,
        after: tuple[int, int] | None = None,
        limit: int = PAGE_SIZE,
        include_after: bool = False,
    ) -> list[Message]:
        if before and after:
            raise ArchiveError("消息翻页参数冲突")
        columns = self._message_columns(conversation.source_table)
        keys=getattr(self,'_message_key_cache',None)
        if keys is None:self._message_key_cache=keys={}
        if conversation.source_table not in keys:
            primary=[row for row in self.message_connection.execute('PRAGMA table_info('+conversation.source_table+')') if row['pk']]
            keys[conversation.source_table]=len(primary)==1 and primary[0]['name']=='msg_id' and primary[0]['type'].upper()=='INTEGER'
        keyed=keys[conversation.source_table]
        conversation_clause, parameters = self._conversation_clause(conversation)
        statement = (
            f"SELECT {'msg_id,timestamp' if keyed else columns} FROM {conversation.source_table} "
            f"WHERE {conversation_clause}"
        )
        if before:
            statement += " AND (timestamp < ? OR (timestamp = ? AND msg_id < ?))"
            parameters.extend((before[0], before[0], before[1]))
            statement += " ORDER BY timestamp DESC, msg_id DESC LIMIT ?"
            parameters.append(limit)
            rows = list(self.message_connection.execute(statement, parameters))
            rows.reverse()
        elif after:
            statement += " AND (timestamp > ? OR (timestamp = ? AND msg_id " + ('>=' if include_after else '>') + " ?))"
            parameters.extend((after[0], after[0], after[1]))
            statement += " ORDER BY timestamp, msg_id LIMIT ?"
            parameters.append(limit)
            rows = list(self.message_connection.execute(statement, parameters))
        else:
            statement += " ORDER BY timestamp DESC, msg_id DESC LIMIT ?"
            parameters.append(limit)
            rows = list(self.message_connection.execute(statement, parameters))
            rows.reverse()
        if keyed and rows:
            ids=[row['msg_id'] for row in rows]
            selected={row['msg_id']:row for row in self.message_connection.execute(
              f'SELECT {columns} FROM {conversation.source_table} WHERE msg_id IN ('+','.join('?' for _ in ids)+')',ids)}
            rows=[selected[identity] for identity in ids]
        return self._rows_to_messages(conversation.source_table, rows)

    @staticmethod
    def _message_columns(source_table: str) -> str:
        base = "msg_id, timestamp, direction, sender_uid, sender_qq, msg_type, text, content"
        if source_table == "c2c_messages":
            return base + ", peer_qq AS chat_qq, peer_uid AS chat_uid"
        if source_table == "group_messages":
            return base + ", group_qq AS chat_qq, group_id AS chat_uid"
        raise ArchiveError(f"不支持的消息源表：{source_table}")

    def _identity_labels_for_messages(
        self, source_table: str, message_ids: Iterable[int]
    ) -> dict[int, tuple[str, str]]:
        """Return optional historic labels keyed by exact structured msg_id."""

        if self.identity_connection is None:
            return {}
        if source_table != "group_messages":
            return {}
        ids = sorted({int(message_id) for message_id in message_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.identity_connection.execute(
            f"""
            SELECT m.msg_id, v.display_name, v.label_source
              FROM group_message_labels AS m
              JOIN label_values AS v ON v.label_id = m.label_id
             WHERE m.msg_id IN ({placeholders})
            """,
            ids,
        )
        return {
            int(row["msg_id"]): (
                _text(row["display_name"]),
                _text(row["label_source"]),
            )
            for row in rows
            if _text(row["display_name"])
        }

    def _snapshot_labels_for_rows(
        self, source_table: str, rows: Iterable[sqlite3.Row]
    ) -> dict[int, tuple[str, str]]:
        """Resolve snapshot labels only after exact historical labels fail."""

        if self.identity_schema != "qq-identity-index/2":
            return {}
        result: dict[int, tuple[str, str]] = {}
        for row in rows:
            if source_table == "c2c_messages":
                label = self._snapshot_lookup_contact(
                    (_text(row["sender_uid"]),),
                    _nonzero_qq(row["sender_qq"]),
                )
            else:
                label = self._snapshot_lookup_group_member(
                    row["chat_qq"],
                    row["chat_uid"],
                    row["sender_uid"],
                    row["sender_qq"],
                )
            if label is not None:
                result[int(row["msg_id"])] = label
        return result

    def _rows_to_messages(
        self, source_table: str, rows: Iterable[sqlite3.Row]
    ) -> list[Message]:
        source_rows = list(rows)
        historical_labels = self._identity_labels_for_messages(
            source_table, (int(row["msg_id"]) for row in source_rows)
        )
        snapshot_labels = self._snapshot_labels_for_rows(source_table, source_rows)
        result: list[Message] = []
        for row in source_rows:
            sender_qq = _nonzero_qq(row["sender_qq"])
            sender_id = (
                str(sender_qq)
                if sender_qq is not None
                else _text(row["sender_uid"])
            )
            sender_id_kind = "QQ" if sender_qq is not None else ("UID" if sender_id else "")
            # Message-local historical card/nickname is always more specific
            # than a later profile/group snapshot.
            label = historical_labels.get(int(row["msg_id"])) or snapshot_labels.get(
                int(row["msg_id"])
            )
            self_sender = sender_qq is not None and str(sender_qq) == self.account_id
            # The pinned NT parser documents C2C direction=1 as sent.  Other
            # direction codes occur in real exports, so sender QQ is primary;
            # group messages deliberately do not use an undocumented fallback.
            sent = self_sender or (
                source_table == "c2c_messages" and int(row["direction"] or -1) == 1
            )
            result.append(
                Message(
                    source_table=source_table,
                    msg_id=int(row["msg_id"]),
                    timestamp=int(row["timestamp"])
                    if row["timestamp"] is not None
                    else None,
                    direction=int(row["direction"])
                    if row["direction"] is not None
                    else None,
                    sender_id=sender_id,
                    sender_id_kind=sender_id_kind,
                    sender_label=label[0] if label else None,
                    sender_label_source=label[1] if label else None,
                    sent=sent,
                    msg_type=int(row["msg_type"])
                    if row["msg_type"] is not None
                    else None,
                    text=_text(row["text"]),
                    content=_text(row["content"]),
                )
            )
        return result

    def latest_messages(self, conversation: Conversation) -> list[Message]:
        return self._message_query(conversation)

    def older_messages(
        self, conversation: Conversation, first_message: Message
    ) -> list[Message]:
        if first_message.timestamp is None:
            return []
        return self._message_query(
            conversation, before=(first_message.timestamp, first_message.msg_id)
        )

    def newer_messages(
        self, conversation: Conversation, last_message: Message
    ) -> list[Message]:
        if last_message.timestamp is None:
            return []
        return self._message_query(
            conversation, after=(last_message.timestamp, last_message.msg_id)
        )

    def messages_around(
        self, conversation: Conversation, msg_id: int, limit: int = PAGE_SIZE
    ) -> list[Message]:
        """Load one bounded context page centered on a full-text search result."""

        conversation_clause, conversation_parameters = self._conversation_clause(
            conversation
        )
        target = self.message_connection.execute(
            f"""
            SELECT timestamp
              FROM {conversation.source_table}
             WHERE {conversation_clause} AND msg_id = ?
            """,
            [*conversation_parameters, msg_id],
        ).fetchone()
        if target is None or target["timestamp"] is None:
            return self.latest_messages(conversation)
        timestamp = int(target["timestamp"])
        half = max(1, limit // 2)
        earlier=self._message_query(conversation,before=(timestamp,msg_id),limit=half)
        later=self._message_query(conversation,after=(timestamp,msg_id),limit=limit-len(earlier),include_after=True)
        return earlier+later

    def _asset(self, relative: str | None, mime: str | None, cache: str | None) -> MediaAsset | None:
        if self.archive_schema == "qq-archive-viewer/2":
            if (
                not relative
                or not relative.startswith("objects/sha256/")
                or self.publication_root is None
            ):
                return None
            candidate = (self.publication_root / Path(relative)).resolve(strict=False)
        else:
            if not relative or not relative.startswith("media/"):
                return None
            candidate = (self.archive_dir / Path(relative)).resolve(strict=False)
        if not _relative_inside(candidate, self.media_root):
            return None
        return MediaAsset(
            archive_relative=relative,
            detected_mime=_text(mime),
            cache_class=_text(cache),
            path=candidate if candidate.is_file() else None,
        )

    def media_for_messages(
        self, source_table: str, messages: Iterable[Message]
    ) -> dict[int, list[MediaLink]]:
        ids = [item.msg_id for item in messages]
        if not ids:
            return {}
        raw_table = RAW_TABLE_FOR_STRUCTURED[source_table]
        placeholders = ",".join("?" for _ in ids)
        query = f"""
            SELECT m.msg_id, m.segment_index, m.media_type, m.role,
                   m.verification, m.note, m.filename,
                   primary_asset.archive_relative AS primary_relative,
                   primary_asset.detected_mime AS primary_mime,
                   primary_asset.cache_class AS primary_cache,
                   preview_asset.archive_relative AS preview_relative,
                   preview_asset.detected_mime AS preview_mime,
                   preview_asset.cache_class AS preview_cache,
                   playable_asset.archive_relative AS playable_relative,
                   playable_asset.detected_mime AS playable_mime,
                   playable_asset.cache_class AS playable_cache,
                   candidate_asset.archive_relative AS candidate_relative,
                   candidate_asset.detected_mime AS candidate_mime,
                   candidate_asset.cache_class AS candidate_cache
              FROM message_media AS m
              LEFT JOIN assets AS primary_asset ON primary_asset.asset_id = m.primary_asset_id
              LEFT JOIN assets AS preview_asset ON preview_asset.asset_id = m.preview_asset_id
              LEFT JOIN assets AS playable_asset ON playable_asset.asset_id = m.playable_asset_id
              LEFT JOIN assets AS candidate_asset ON candidate_asset.asset_id = m.candidate_asset_id
             WHERE m.source_table = ? AND m.msg_id IN ({placeholders})
             ORDER BY m.msg_id, m.segment_index, m.link_id
        """
        result: dict[int, list[MediaLink]] = {}
        for row in self.map_connection.execute(query, [raw_table, *ids]):
            link = MediaLink(
                source_table=source_table,
                msg_id=int(row["msg_id"]),
                segment_index=int(row["segment_index"]),
                media_type=_text(row["media_type"]),
                role=_text(row["role"]),
                verification=_text(row["verification"]),
                note=_text(row["note"]),
                filename=_text(row["filename"]) or None,
                primary=self._asset(
                    row["primary_relative"], row["primary_mime"], row["primary_cache"]
                ),
                preview=self._asset(
                    row["preview_relative"], row["preview_mime"], row["preview_cache"]
                ),
                playable=self._asset(
                    row["playable_relative"], row["playable_mime"], row["playable_cache"]
                ),
                candidate=self._asset(
                    row["candidate_relative"], row["candidate_mime"], row["candidate_cache"]
                ),
            )
            result.setdefault(link.msg_id, []).append(link)
        return result

    def _search_rows(self, source_table, qq_column, uid_column, phrase, row_limit):
        return list(self.message_connection.execute(
            f'''SELECT msg_id,timestamp,direction,sender_qq,sender_uid,text,
                {qq_column} AS chat_qq,{uid_column} AS chat_uid FROM {source_table}
                WHERE instr(lower(coalesce(text,'')),lower(?))>0
                ORDER BY timestamp DESC,msg_id DESC LIMIT ?''', (phrase,row_limit)))

    def search(
        self, query: str, limit: int = 200, *, offset: int = 0,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[SearchHit]:
        """Literal substring search, globally ordered and paged across both tables.

        unicode61 token hits are not a complete set of Chinese substring hits.
        Do not conditionally use LIKE only when MATCH happens to return no rows.
        instr also treats %, _ and backslash as ordinary user characters.
        """
        phrase = query.strip()
        if len(phrase) < 2:
            return []
        if limit < 1 or offset < 0:
            raise ValueError("invalid search page")
        results: list[SearchHit] = []
        configurations = (
            (
                "c2c_messages",
                "c2c_messages_fts",
                "peer_qq",
                "peer_uid",
                "私聊",
            ),
            (
                "group_messages",
                "group_messages_fts",
                "group_qq",
                "group_id",
                "群聊",
            ),
        )
        for source_table, fts_table, qq_column, uid_column, kind in configurations:
            self.message_connection.set_progress_handler(
                (lambda: int(cancelled())) if cancelled else None, 10000
            )
            try:
                rows = self._search_rows(source_table,qq_column,uid_column,phrase,offset+limit)
            finally:
                self.message_connection.set_progress_handler(None, 0)
            historical_labels = self._identity_labels_for_messages(
                source_table, (int(row["msg_id"]) for row in rows)
            )
            snapshot_labels = self._snapshot_labels_for_rows(source_table, rows)
            for row in rows:
                conversation = self._conversation_for_identity(
                    source_table,
                    kind,
                    qq_column,
                    uid_column,
                    row["chat_qq"],
                    row["chat_uid"],
                )
                sender_qq = _nonzero_qq(row["sender_qq"])
                sender_id = (
                    str(sender_qq)
                    if sender_qq is not None
                    else _text(row["sender_uid"])
                )
                label = historical_labels.get(int(row["msg_id"])) or snapshot_labels.get(
                    int(row["msg_id"])
                )
                results.append(
                    SearchHit(
                        conversation=conversation,
                        msg_id=int(row["msg_id"]),
                        timestamp=int(row["timestamp"]) if row["timestamp"] else None,
                        sender_id=sender_id,
                        sender_id_kind=(
                            "QQ" if sender_qq is not None else ("UID" if sender_id else "")
                        ),
                        sender_label=label[0] if label else None,
                        sender_label_source=label[1] if label else None,
                        preview=_safe_summary(_text(row["text"])),
                    )
                )
        results.sort(key=lambda item: (item.timestamp or 0, item.msg_id, item.conversation.source_table), reverse=True)
        return results[offset:offset + limit]


def open_archive(manifest_path: Path, account_label=None, **kwargs):
    document=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if document.get('schema') in {'qq-next-viewer/1','qq-next-viewer/2'}:
        from qq_archive_next.reader import NextArchiveStore
        return NextArchiveStore(manifest_path,account_label,**kwargs)
    return ArchiveStore(manifest_path,account_label,**kwargs)
