from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_store import ArchiveError


@dataclass(frozen=True, slots=True)
class PublicationAccount:
    display_id: str
    manifest_path: Path
    logical_messages: int
    source_runs: int
    account_key: str = ""
    selectors: tuple[str, ...] = ()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_file(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ArchiveError("统一发布清单包含无效相对路径")
    if "\\" in value or ":" in value or ".." in Path(value).parts:
        raise ArchiveError("统一发布清单包含不安全路径")
    candidate = (root / value).resolve(strict=True)
    if not _inside(candidate, root) or not candidate.is_file():
        raise ArchiveError("统一发布清单中的账号归档不存在或越界")
    return candidate


def publication_accounts(path: Path) -> list[PublicationAccount]:
    manifest_path = path.resolve(strict=True)
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"无法读取归档清单：{exc}") from exc
    if not isinstance(document, dict):
        raise ArchiveError("归档清单根节点无效")
    schema = document.get("schema")
    if schema in {'qq-next-viewer/1','qq-next-viewer/2'}:
        publication=manifest_path.parent/'viewer_manifest.json'
        if publication!=manifest_path and publication.is_file():
            matches=[item for item in publication_accounts(publication)
              if item.account_key==document.get('account_key') and item.manifest_path==manifest_path]
            if len(matches)==1:return matches
    if schema in {"qq-archive-viewer/1", "qq-archive-viewer/2", "qq-next-viewer/1", "qq-next-viewer/2"}:
        storage_id = str(
            document.get("account_id")
            or document.get("storage_account_id")
            or "未知账号"
        )
        return [
            PublicationAccount(
                display_id=storage_id,
                manifest_path=manifest_path,
                logical_messages=int(
                    document.get("canonical_publication", {}).get("logical_messages", 0)
                ),
                source_runs=int(
                    document.get("canonical_publication", {}).get("source_runs", 1)
                ),
                account_key=str(document.get("account_key") or ""),
                selectors=tuple(
                    value
                    for value in (storage_id, str(document.get("account_key") or ""))
                    if value
                ),
            )
        ]
    next_schema = schema in {"qq-next-publication/1","qq-next-publication/2"} and document.get("status") == "active"
    if not next_schema and (schema not in {"qq-canonical-publication/1", "qq-canonical-publication/2"} or document.get("status") != "verified"):
        raise ArchiveError("不支持的归档或统一发布清单格式")
    accounts = document.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ArchiveError("统一发布清单没有可打开账号")
    private_labels: dict[str, str] = {}
    if schema in {"qq-canonical-publication/2", "qq-next-publication/1", "qq-next-publication/2"}:
        private = document.get("private")
        if not isinstance(private, dict):
            raise ArchiveError("v2 统一发布缺少私密账号标签侧车声明")
        labels_path = _relative_file(
            manifest_path.parent, private.get("account_labels_sidecar")
        )
        try:
            labels_doc = json.loads(labels_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"无法读取私密账号标签侧车：{exc}") from exc
        if (
            not isinstance(labels_doc, dict)
            or labels_doc.get("schema") != "qq-account-labels-private/1"
        ):
            raise ArchiveError("私密账号标签侧车格式无效")
        for entry in labels_doc.get("accounts", []):
            if not isinstance(entry, dict):
                raise ArchiveError("私密账号标签条目无效")
            account_key = str(entry.get("account_key") or "")
            account_label = str(entry.get("account_label") or "")
            if not account_key or not account_label or not account_label.isdecimal():
                raise ArchiveError("私密账号标签条目缺少有效 QQ 号")
            if account_key in private_labels or account_label in private_labels.values():
                raise ArchiveError("私密账号标签侧车包含重复项")
            private_labels[account_key] = account_label

    result: list[PublicationAccount] = []
    for item in accounts:
        if not isinstance(item, dict):
            raise ArchiveError("统一发布清单账号条目无效")
        messages = item.get("messages", {})
        account_key = str(item.get("account_key") or "")
        legacy_display = str(item.get("display_id") or "")
        if schema in {"qq-canonical-publication/2", "qq-next-publication/1", "qq-next-publication/2"}:
            if account_key not in private_labels:
                raise ArchiveError("v2 统一发布账号缺少私密 QQ 号映射")
            display_id = private_labels[account_key]
        else:
            display_id = legacy_display or "未知账号"
        result.append(
            PublicationAccount(
                display_id=display_id,
                manifest_path=_relative_file(
                    manifest_path.parent, item.get("archive_manifest")
                ),
                logical_messages=int(messages.get("logical_messages", 0)),
                source_runs=int(item.get("source_runs", 0)),
                account_key=account_key,
                selectors=tuple(
                    dict.fromkeys(
                        value
                        for value in (display_id, account_key, legacy_display)
                        if value
                    )
                ),
            )
        )
    if schema in {"qq-canonical-publication/2", "qq-next-publication/1", "qq-next-publication/2"} and set(private_labels) != {
        item.account_key for item in result
    }:
        raise ArchiveError("私密账号标签侧车与 v2 发布账号集合不一致")
    display_ids = [item.display_id for item in result]
    if len(display_ids) != len(set(display_ids)):
        raise ArchiveError("统一发布清单包含重复账号显示标识")
    return result
