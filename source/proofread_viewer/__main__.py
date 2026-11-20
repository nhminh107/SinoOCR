"""CLI entry point for running the per-page 3-column submission proofreading UI."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .server import create_server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m source.proofread_viewer",
        description="UI hiệu đính 3 cột theo từng trang: Hình ảnh - han/<page>.txt - viet/<page>.txt (03_text_sach).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8768,
        help="Cổng localhost (mặc định: 8768).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không tự động mở trình duyệt khi khởi động.",
    )
    parser.add_argument(
        "--submission-dir",
        type=Path,
        default=Path("submission"),
        help="Thư mục submission (mặc định: submission).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = create_server(
        host="127.0.0.1",
        port=args.port,
        submission_dir=args.submission_dir,
    )
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"

    datasets = server.index.list_datasets()
    print(f"UI Hiệu đính theo từng trang đang chạy tại: {url}")
    for ds in datasets:
        print(
            f"  - {ds['label']}: {ds['page_count']} ảnh | "
            f"03_text_sach/han: {ds['han_03_count']} | "
            f"03_text_sach/viet: {ds['viet_03_count']}"
        )
    print("Nhấn Ctrl+C để dừng.")

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng UI Hiệu đính.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
