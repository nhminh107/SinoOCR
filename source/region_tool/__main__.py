"""CLI mở giao diện web duyệt vùng OCR trên máy cục bộ."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .geometry import DEFAULT_FOLDERS, PROJECT_DIR, collect_images

DEFAULT_MODEL_DIR = Path.home() / ".paddlex" / "official_models" / "PP-OCRv6_medium_det"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m source.region_tool",
        description=(
            "Giao diện web cục bộ nhận diện (PP-OCRv6_medium_det) và gán nhãn vùng chữ "
            "theo cột/dòng cho tư liệu Hán Nôm và chữ Quốc ngữ."
        ),
    )
    parser.add_argument(
        "folders",
        nargs="*",
        type=Path,
        default=DEFAULT_FOLDERS,
        help="Các thư mục ảnh đầu vào (mặc định quét cả 01_QuocTrieuHuongKhoaLuc và 02_TuLiemDaiKhoaLuc).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_DIR / "outputs" / "regions",
        help="Thư mục lưu JSON annotation và session.json (mặc định: outputs/regions).",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help="Đường dẫn thư mục cache model PP-OCRv6_medium_det cục bộ.",
    )
    parser.add_argument(
        "--engine",
        choices=("paddle", "onnxruntime"),
        default="paddle",
        help="Engine suy luận cục bộ: 'paddle' (mặc định) hoặc 'onnxruntime' (nếu có thư mục ONNX).",
    )
    parser.add_argument(
        "--device",
        default="gpu:0",
        help="Thiết bị suy luận (mặc định: gpu:0).",
    )
    parser.add_argument("--port", type=int, default=8765, help="Cổng localhost (mặc định: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    entries = collect_images(args.folders)
    if not entries:
        parser.error("Không tìm thấy ảnh hợp lệ trong các thư mục đã chọn.")

    from .server import RegionWorkspace, launch_server

    workspace = RegionWorkspace(
        entries, args.output_dir, model_dir=args.model_dir, engine=args.engine, device=args.device,
    )
    try:
        launch_server(workspace, port=args.port, open_browser=not args.no_browser)
    except OSError as exc:
        parser.exit(1, f"Không mở được máy chủ localhost: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
