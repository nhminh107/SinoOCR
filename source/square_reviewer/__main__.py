"""CLI entry point for the square-character OCR reviewer UI."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mở UI kiểm định các trang/dòng có dấu □ và cập nhật nhật ký CSV."
    )
    parser.add_argument(
        "--submission-dir",
        type=Path,
        default=Path("submission"),
        help="Thư mục submission chứa các tác phẩm (mặc định: submission).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"Square Reviewer UI đang chạy tại: {url}")
    print("Nhấn Ctrl+C để dừng.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    run_server(args.submission_dir, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
