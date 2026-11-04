#!/usr/bin/env python3
"""Content-identity matching policy for QQ media-map/2.

The module is deliberately independent from the NTQQ protobuf parser.  It can
reclassify an immutable media-map/1 observation set after the archived asset
bytes have been rehashed.  A filename or recorded path can remain a candidate,
but only an exact byte MD5 plus a content-type gate can become verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


SCHEMA = "qq-media-map/2"
RULE_VERSION = "media-map-v2-byte-md5-mime-gate/1"
SAFE_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/gif"})
SAFE_VIDEO_MIMES = frozenset({"video/mp4"})
SAFE_VOICE_MIMES = frozenset(
    {"audio/x-qq-silk", "audio/wav", "audio/mpeg", "audio/aac", "audio/ogg"}
)


@dataclass(frozen=True)
class Asset:
    asset_id: int
    asset_kind: str
    archive_relative: str
    md5: str | None
    detected_mime: str
    cache_class: str
    source_relative: str | None = None


@dataclass(frozen=True)
class Decision:
    role: str
    primary_asset_id: int | None
    preview_asset_id: int | None
    playable_asset_id: int | None
    candidate_asset_id: int | None
    verification: str
    note: str
    match_method: str
    type_gate: str


def _priority(asset: Asset) -> tuple[int, str, int]:
    """Choose deterministically when identical bytes occur in several caches."""

    order = {
        "image_original_candidate": 0,
        "image_cache": 1,
        "attachment_cache": 2,
        "video_original_candidate": 3,
        "video_cache": 4,
        "video_thumbnail": 5,
        "image_thumbnail_candidate": 6,
        "attachment_preview_candidate": 7,
        "emoji_cache": 8,
        "voice_silk_or_audio": 9,
        "other_cache": 10,
        "decoded_voice_wav": 20,
    }
    return (order.get(asset.cache_class, 15), asset.archive_relative, asset.asset_id)


def first_matching(
    assets: Iterable[Asset],
    *,
    allowed_mimes: frozenset[str] | None = None,
    raw_only: bool = True,
) -> Asset | None:
    candidates = [
        item
        for item in assets
        if (not raw_only or item.asset_kind == "raw_cache")
        and (allowed_mimes is None or item.detected_mime in allowed_mimes)
    ]
    return min(candidates, key=_priority) if candidates else None


def _legacy_candidate(row: Mapping[str, object]) -> tuple[int | None, str, str]:
    candidate = row.get("candidate_asset_id")
    primary = row.get("primary_asset_id")
    verification = str(row.get("verification") or "")
    if candidate is not None:
        return int(candidate), "filename_candidate", "filename_only"
    if primary is not None and verification == "verified_path":
        return int(primary), "path_candidate", "recorded_path_only"
    return None, "no_exact_local_content", "none"


def decide(
    row: Mapping[str, object],
    assets_by_md5: Mapping[str, list[Asset]],
    assets_by_id: Mapping[int, Asset],
) -> Decision:
    """Return one evidence-labelled v2 mapping decision for a v1 observation."""

    kind = str(row["media_type"])
    primary_md5 = str(row.get("primary_md5") or "").lower()
    thumbnail_md5 = str(row.get("thumbnail_md5") or "").lower()
    primary_pool = assets_by_md5.get(primary_md5, []) if primary_md5 else []
    thumbnail_pool = assets_by_md5.get(thumbnail_md5, []) if thumbnail_md5 else []

    if kind == "image":
        exact = first_matching(primary_pool, allowed_mimes=SAFE_IMAGE_MIMES)
        if exact:
            return Decision(
                "image_original", exact.asset_id, None, None, None,
                "verified_content",
                "Actual archived bytes matched the message MD5 and passed the JPEG/PNG/GIF header gate.",
                "exact_byte_md5", "safe_image_magic_mime",
            )
        conflict = first_matching(primary_pool)
        if conflict:
            return Decision(
                "image_type_conflict", None, None, None, conflict.asset_id,
                "type_conflict_quarantined",
                "Actual archived bytes matched the message MD5 but did not pass the safe image type gate.",
                "exact_byte_md5", "quarantined_non_image_mime",
            )

    elif kind == "video":
        exact = first_matching(primary_pool, allowed_mimes=SAFE_VIDEO_MIMES)
        preview = first_matching(thumbnail_pool, allowed_mimes=SAFE_IMAGE_MIMES)
        if exact:
            return Decision(
                "video_original", exact.asset_id,
                preview.asset_id if preview else None, exact.asset_id, None,
                "verified_content",
                "Actual archived bytes matched the message MD5 and passed the video type gate.",
                "exact_byte_md5", "safe_video_magic_mime",
            )
        conflict = first_matching(primary_pool)
        if conflict:
            return Decision(
                "video_type_conflict", None,
                preview.asset_id if preview else None, None, conflict.asset_id,
                "type_conflict_quarantined",
                "Actual archived bytes matched the message MD5 but did not pass the video type gate.",
                "exact_byte_md5", "quarantined_non_video_mime",
            )
        if preview:
            return Decision(
                "video_thumbnail", None, preview.asset_id, None, None,
                "preview_only",
                "The thumbnail MD5 matched safe image bytes; the video original was not verified locally.",
                "exact_thumbnail_byte_md5", "safe_image_magic_mime",
            )

    elif kind == "attachment":
        exact = first_matching(primary_pool)
        if exact:
            return Decision(
                "attachment_original", exact.asset_id, None, None, None,
                "verified_content",
                "Actual archived bytes matched the attachment message MD5; rendering remains MIME-specific.",
                "exact_byte_md5", "generic_attachment_bytes",
            )

    elif kind == "voice":
        exact = first_matching(primary_pool, allowed_mimes=SAFE_VOICE_MIMES)
        if exact:
            playable_id = row.get("playable_asset_id")
            playable = assets_by_id.get(int(playable_id)) if playable_id is not None else None
            if playable is not None and playable.asset_kind != "derived_wav":
                playable = None
            return Decision(
                "voice_original", exact.asset_id, None,
                playable.asset_id if playable else None, None,
                "verified_content",
                "Actual archived bytes matched the voice message MD5; a linked WAV remains a rebuildable derivative.",
                "exact_byte_md5", "safe_voice_magic_mime",
            )

    elif kind == "emoji":
        exact = first_matching(primary_pool, allowed_mimes=SAFE_IMAGE_MIMES)
        if exact:
            return Decision(
                "emoji_original", exact.asset_id, None, None, None,
                "verified_content",
                "Actual archived bytes matched the emoji message MD5 and passed the safe image type gate.",
                "exact_byte_md5", "safe_image_magic_mime",
            )

    candidate_id, status, method = _legacy_candidate(row)
    if candidate_id is not None and candidate_id not in assets_by_id:
        candidate_id = None
        status, method = "no_exact_local_content", "none"
    if status == "filename_candidate":
        note = "Filename candidate only; it is not a content-verified relationship."
    elif status == "path_candidate":
        note = "Recorded local path candidate only; path existence is not content verification."
    else:
        note = "No eligible exact local content match exists in this frozen archive."
    return Decision(
        f"{kind}_candidate" if candidate_id is not None else f"{kind}_unavailable",
        None, None, None, candidate_id, status, note, method, "none",
    )
