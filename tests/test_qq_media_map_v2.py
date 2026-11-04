from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qq_media_map_v2 import Asset, decide  # noqa: E402


def asset(asset_id: int, md5: str, mime: str, cache: str = "image_cache") -> Asset:
    return Asset(asset_id, "raw_cache", f"media/{asset_id}", md5, mime, cache)


def row(kind: str, md5: str = "aa", **values: object) -> dict[str, object]:
    return {
        "media_type": kind,
        "primary_md5": md5,
        "thumbnail_md5": values.pop("thumbnail_md5", None),
        "candidate_asset_id": values.pop("candidate_asset_id", None),
        "primary_asset_id": values.pop("primary_asset_id", None),
        "playable_asset_id": values.pop("playable_asset_id", None),
        "verification": values.pop("verification", "missing_local"),
        **values,
    }


class MediaMapV2PolicyTests(unittest.TestCase):
    def test_image_cache_exact_md5_and_safe_magic_is_verified(self) -> None:
        item = asset(1, "aa", "image/jpeg")
        result = decide(row("image"), {"aa": [item]}, {1: item})
        self.assertEqual(result.verification, "verified_content")
        self.assertEqual(result.primary_asset_id, 1)
        self.assertEqual(result.match_method, "exact_byte_md5")

    def test_filename_match_never_becomes_verified(self) -> None:
        item = asset(2, "bb", "image/png")
        result = decide(
            row("image", "aa", candidate_asset_id=2, verification="filename_candidate"),
            {"bb": [item]},
            {2: item},
        )
        self.assertEqual(result.verification, "filename_candidate")
        self.assertIsNone(result.primary_asset_id)
        self.assertEqual(result.candidate_asset_id, 2)

    def test_recorded_path_is_only_a_candidate(self) -> None:
        item = asset(3, "bb", "image/png")
        result = decide(
            row("image", "aa", primary_asset_id=3, verification="verified_path"),
            {"bb": [item]},
            {3: item},
        )
        self.assertEqual(result.verification, "path_candidate")
        self.assertIsNone(result.primary_asset_id)
        self.assertEqual(result.candidate_asset_id, 3)

    def test_image_md5_with_mp4_bytes_is_quarantined(self) -> None:
        item = asset(4, "aa", "video/mp4", "image_cache")
        result = decide(row("image"), {"aa": [item]}, {4: item})
        self.assertEqual(result.verification, "type_conflict_quarantined")
        self.assertIsNone(result.primary_asset_id)
        self.assertEqual(result.candidate_asset_id, 4)

    def test_video_cache_exact_mp4_is_verified(self) -> None:
        item = asset(5, "aa", "video/mp4", "video_cache")
        result = decide(row("video"), {"aa": [item]}, {5: item})
        self.assertEqual(result.verification, "verified_content")
        self.assertEqual(result.playable_asset_id, 5)

    def test_attachment_cache_exact_md5_is_verified_for_download(self) -> None:
        item = asset(6, "aa", "application/octet-stream", "attachment_cache")
        result = decide(row("attachment"), {"aa": [item]}, {6: item})
        self.assertEqual(result.verification, "verified_content")
        self.assertEqual(result.type_gate, "generic_attachment_bytes")

    def test_video_thumbnail_is_preview_only(self) -> None:
        preview = asset(7, "tt", "image/jpeg", "video_thumbnail")
        result = decide(
            row("video", thumbnail_md5="tt"), {"tt": [preview]}, {7: preview}
        )
        self.assertEqual(result.verification, "preview_only")
        self.assertEqual(result.preview_asset_id, 7)
        self.assertIsNone(result.primary_asset_id)

    def test_no_match_is_not_called_missing_file(self) -> None:
        result = decide(row("image"), {}, {})
        self.assertEqual(result.verification, "no_exact_local_content")


if __name__ == "__main__":
    unittest.main()
