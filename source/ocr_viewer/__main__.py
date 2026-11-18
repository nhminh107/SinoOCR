"""CLI entry point for running the local OCR viewer."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .server import create_server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m source.ocr_viewer",
        description="Trình xem ảnh và kết quả OCR Hán–Nôm / Việt cục bộ.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8767,
        help="Cổng localhost (mặc định: 8767).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không tự động mở trình duyệt khi khởi động.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Thư mục chứa ảnh nguồn (mặc định: data).",
    )
    parser.add_argument(
        "--submission-dir",
        type=Path,
        default=Path("submission"),
        help="Thư mục chứa kết quả OCR (mặc định: submission).",
    )
    parser.add_argument(
        "--review-file",
        type=Path,
        default=Path("outputs/ocr_review.json"),
        help="File JSON lưu đánh dấu và ghi chú (mặc định: outputs/ocr_review.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = create_server(
        host="127.0.0.1",
        port=args.port,
        data_dir=args.data_dir,
        submission_dir=args.submission_dir,
        review_file=args.review_file,
    )
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"

    datasets = server.index.list_datasets()
    total_images = sum(ds["count"] for ds in datasets)
    print(f"OCR Viewer đang chạy tại: {url}")
    print(f"Tác phẩm: {len(datasets)} | Tổng số ảnh: {total_images}")
    print(f"File ghi chú: {args.review_file}")
    print("Nhấn Ctrl+C để dừng.")

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng OCR Viewer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
