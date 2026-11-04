from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .app import MainWindow, create_application
from .archive_store import ArchiveError, ArchiveStore, open_archive
from .publication import publication_accounts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读 QQ NT 聊天记录与媒体归档查看器")
    parser.add_argument("--archive", type=Path, help="archive_manifest.json")
    parser.add_argument("--account", help="统一发布中的真实 QQ 号、account_key 或 v1 显示标识")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="验证归档契约并输出计数，不启动图形界面",
    )
    parser.add_argument(
        "--gui-smoke",
        action="store_true",
        help="离屏构造主窗口并加载指定账号后立即关闭",
    )
    args = parser.parse_args(argv)
    if args.smoke:
        if not args.archive:
            parser.error("--smoke requires --archive")
        try:
            candidates = publication_accounts(args.archive)
            if args.account:
                candidates = [item for item in candidates if args.account in item.selectors]
                if len(candidates) != 1:
                    raise ArchiveError("统一发布清单中找不到指定账号")
            overviews = []
            for candidate in candidates:
                store = open_archive(
                    candidate.manifest_path, account_label=candidate.display_id, deep_validation=True
                )
                try:
                    overviews.append(store.overview())
                finally:
                    store.close()
            print(json.dumps({"status": "ok", "accounts": overviews}, ensure_ascii=False))
            return 0
        except (ArchiveError, FileNotFoundError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.gui_smoke:
        if not args.archive or not args.account:
            parser.error("--gui-smoke requires --archive and --account")
        application = create_application()
        window = MainWindow(args.archive, args.account)
        if window.store is None:
            return 2
        deadline = time.monotonic() + 180
        while window._reads and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        if window._reads or window.store._conversation_cache is None:
            window.close()
            return 2
        conversations = window.store._conversation_cache
        if conversations:
            window.select_conversation(conversations[0])
            while window._reads and time.monotonic() < deadline:
                application.processEvents()
                time.sleep(0.005)
            if window._reads or not window.current_messages:
                window.close()
                return 2
        overview = window.store.overview()
        window.close()
        while window._pool.activeThreadCount():
            application.processEvents()
            time.sleep(0.005)
        print(json.dumps({"status": "ok", "account": overview["account_id"], "conversations": len(conversations), "messages_loaded": len(window.current_messages)}))
        return 0
    application = create_application()
    window = MainWindow(args.archive, args.account)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
