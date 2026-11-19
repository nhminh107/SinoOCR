"""CLI cho gói source.LayoutAnalysis: hỗ trợ chạy batch toàn bộ ảnh, chạy lẻ 1 ảnh, hoặc mở giao diện web."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .detection import DEFAULT_MODEL_DIR, SUPPORTED_ENGINES
from .geometry import DEFAULT_FOLDERS, PROJECT_DIR, collect_images
from .layout_analysis import SinoLayoutDetector, render_layout_image


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m source.LayoutAnalysis",
        description=(
            "Phân tích bố cục tư liệu Hán Nôm & chữ Quốc ngữ (PP-OCRv6_medium_det + HisDoc1B), "
            "xuất JSON tổng toàn bộ ảnh hoặc khởi chạy giao diện web cục bộ."
        ),
    )
    parser.add_argument(
        "folders",
        nargs="*",
        type=Path,
        default=DEFAULT_FOLDERS,
        help="Các thư mục hoặc tệp ảnh đầu vào (mặc định quét cả 01_QuocTrieuHuongKhoaLuc và 02_TuLiemDaiKhoaLuc).",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Chạy pipeline Layout Analysis cho toàn bộ ảnh trong folders và xuất file JSON tổng.",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=PROJECT_DIR / "results" / "layout_all.json",
        help="Đường dẫn file JSON tổng cho toàn bộ ảnh (mặc định: results/layout_all.json).",
    )
    parser.add_argument(
        "--save-vis-dir",
        type=Path,
        default=None,
        help="Thư mục lưu ảnh visualize hàng loạt khi chạy --batch (tùy chọn).",
    )
    parser.add_argument(
        "--overwrite-reviewed",
        action="store_true",
        help="Ghi đè cả các file JSON từng trang đã được đánh dấu 'reviewed' trong --output-dir.",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Bao gồm cả raw_detections trong file JSON tổng.",
    )
    parser.add_argument(
        "--predict",
        type=Path,
        default=None,
        help="Chạy phân tích Layout trực tiếp trên một ảnh cụ thể và xuất JSON/ảnh minh họa.",
    )
    parser.add_argument(
        "--save-vis",
        type=Path,
        default=None,
        help="Đường dẫn lưu ảnh visualize kết quả khi dùng --predict.",
    )
    parser.add_argument(
        "--save-json",
        type=Path,
        default=None,
        help="Đường dẫn lưu JSON kết quả khi dùng --predict (hoặc ghi đè đường dẫn JSON tổng khi chạy --batch).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_DIR / "outputs" / "regions",
        help="Thư mục lưu JSON annotation từng ảnh và session.json (mặc định: outputs/regions).",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help="Đường dẫn thư mục cache model PP-OCRv6_medium_det cục bộ.",
    )
    parser.add_argument(
        "--engine",
        choices=SUPPORTED_ENGINES,
        default="paddle",
        help="Engine suy luận cục bộ: 'paddle' (mặc định), 'onnxruntime', 'projection', hoặc 'auto'.",
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

    if args.predict is not None:
        if not args.predict.is_file():
            parser.error(f"Không tìm thấy tệp ảnh: {args.predict}")
        engine = "auto" if args.engine == "paddle" and not args.model_dir.exists() else args.engine
        detector = SinoLayoutDetector(
            device=args.device,
            model_dir=args.model_dir,
            engine=engine,
            annotations_dir=args.output_dir,
        )
        result = detector.predict(args.predict, save_json_path=args.save_json)
        if args.save_vis is not None:
            vis = render_layout_image(args.predict, result)
            args.save_vis.parent.mkdir(parents=True, exist_ok=True)
            vis.save(args.save_vis)
        print(
            f"Đã phân tích '{result.relative_path or args.predict.name}': {len(result.bboxes)} vùng "
            f"(direction={result.script_direction}, header_line_y={result.header_line_y})"
        )
        for box in result.bboxes:
            b = [round(v) for v in box.bbox]
            print(f"  #{box.reading_order:2d} [{box.label:6s}] bbox={b} conf={box.confidence:.2f}")
        return 0

    if args.batch:
        engine = "auto" if args.engine == "paddle" and not args.model_dir.exists() else args.engine
        detector = SinoLayoutDetector(
            device=args.device,
            model_dir=args.model_dir,
            engine=engine,
            annotations_dir=args.output_dir,
        )
        summary_target = args.save_json if args.save_json is not None else args.summary_json
        detector.predict_batch(
            args.folders,
            summary_json_path=summary_target,
            per_page_output_dir=args.output_dir,
            vis_output_dir=args.save_vis_dir,
            overwrite_reviewed=args.overwrite_reviewed,
            include_raw_in_summary=args.include_raw,
            verbose=True,
        )
        return 0

    entries = collect_images(args.folders)
    if not entries:
        parser.error("Không tìm thấy ảnh hợp lệ trong các thư mục đã chọn.")

    from .server import RegionWorkspace, launch_server

    engine = "auto" if args.engine == "paddle" and not args.model_dir.exists() else args.engine
    workspace = RegionWorkspace(
        entries,
        args.output_dir,
        model_dir=args.model_dir,
        engine=engine,
        device=args.device,
        summary_json_path=args.summary_json,
    )
    try:
        launch_server(workspace, port=args.port, open_browser=not args.no_browser)
    except OSError as exc:
        parser.exit(1, f"Không mở được máy chủ localhost: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
