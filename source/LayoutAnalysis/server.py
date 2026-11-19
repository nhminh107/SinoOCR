"""Máy chủ web cục bộ cho giao diện duyệt vùng LayoutAnalysis; chỉ dùng thư viện chuẩn Python."""

from __future__ import annotations

import json
import math
import mimetypes
import re
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .detection import DEFAULT_MODEL_DIR, RegionDetector
from .geometry import (
    CONTENT_ROLES, DIRECTIONS, LABELS, LANGUAGES, PROJECT_DIR, REVIEW_STATES,
    ImageEntry, bulk_number_regions, clamp_bbox, group_detections,
    merge_regions, normalize_orientation, split_region,
)
from .persistence import (
    PageAnnotation, export_crops, load_annotation, load_session,
    new_annotation_for_entry, save_annotation, save_session, update_summary_json,
)

STATIC_DIR = Path(__file__).parent / "static"


class RegionWorkspace:
    def __init__(
        self, entries: list[ImageEntry], output_dir: Path, *,
        model_dir: Path = DEFAULT_MODEL_DIR, engine: str = "paddle", device: str = "gpu:0",
        summary_json_path: Path | None = PROJECT_DIR / "results" / "layout_all.json",
    ) -> None:
        self.entries = entries
        self.output_dir = Path(output_dir)
        self.summary_json_path = Path(summary_json_path) if summary_json_path is not None else None
        self.detector = RegionDetector(
            model_dir=model_dir, engine=engine, device=device,
            cache_dir=self.output_dir / ".cache" / "paddlex",
        )
        self.jobs: dict[str, dict] = {}
        self.detection_lock = threading.Lock()
        self.save_lock = threading.Lock()

    def entry(self, image_id: int) -> ImageEntry:
        if image_id < 0 or image_id >= len(self.entries):
            raise ValueError("Không tìm thấy ảnh trong hai thư mục đã chọn.")
        return self.entries[image_id]

    def bootstrap(self) -> dict:
        session = load_session(self.output_dir) or {}
        current_id = next(
            (i for i, e in enumerate(self.entries) if e.key == session.get("last_image_key")), 0
        )
        return {
            "images": [
                {"id": i, "key": e.key, "dataset": e.dataset, "filename": e.filename,
                 "page_index": e.page_index, "image_part": e.image_part,
                 "pdf_basename": e.pdf_source.pdf_basename if e.pdf_source else None,
                 "pdf_page_index": e.pdf_page_index}
                for i, e in enumerate(self.entries)
            ],
            "current_id": current_id,
            "config": {"model_name": self.detector.model_name, "device": self.detector.device,
                       "model_dir": str(self.detector.model_dir)},
            "output_dir": str(self.output_dir),
        }

    def load(self, image_id: int) -> PageAnnotation:
        entry = self.entry(image_id)
        with Image.open(entry.path) as image:
            return load_annotation(self.output_dir, entry, *image.size)

    def parse_annotation(self, image_id: int, payload: dict) -> PageAnnotation:
        entry = self.entry(image_id)
        with Image.open(entry.path) as image:
            width, height = image.size
        if (payload.get("image_width"), payload.get("image_height")) != (width, height):
            raise ValueError("Kích thước annotation không khớp ảnh gốc.")
        if (payload.get("dataset"), payload.get("filename")) != (entry.dataset, entry.filename):
            raise ValueError("Annotation không thuộc ảnh đang mở.")
        annotation = PageAnnotation.from_dict(payload)
        source = new_annotation_for_entry(entry, width, height)
        for attr in ("image_path", "page_index", "image_part", "pdf_basename", "pdf_page_range", "pdf_page_index"):
            setattr(annotation, attr, getattr(source, attr))
        if annotation.page_review_state not in REVIEW_STATES:
            raise ValueError("Trạng thái trang không hợp lệ.")
        ids = set()
        for region in annotation.regions:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", region.id) or region.id in ids:
                raise ValueError("Mã vùng phải duy nhất và chỉ gồm chữ, số, gạch nối hoặc gạch dưới.")
            ids.add(region.id)
            for value, choices in (
                (region.label, LABELS), (region.language, LANGUAGES),
                (region.content_role, CONTENT_ROLES), (region.direction, DIRECTIONS),
                (region.review_state, REVIEW_STATES),
            ):
                if value not in choices:
                    raise ValueError("Thuộc tính vùng không hợp lệ.")
            if len(region.bbox) != 4 or not all(math.isfinite(v) for v in region.bbox):
                raise ValueError("Tọa độ vùng không hợp lệ.")
            region.bbox = clamp_bbox(region.bbox, width, height)
            if region.bbox[2] <= region.bbox[0] or region.bbox[3] <= region.bbox[1]:
                raise ValueError("Vùng chữ phải có chiều rộng và chiều cao lớn hơn 0.")
        return annotation

    def save(self, image_id: int, payload: dict) -> dict:
        annotation = self.parse_annotation(image_id, payload)
        entry = self.entry(image_id)
        with self.save_lock:
            self.load(image_id)
            page_path = save_annotation(self.output_dir, entry, annotation)
            save_session(self.output_dir, entry, image_id)
            if self.summary_json_path is not None:
                update_summary_json(self.summary_json_path, entry, annotation)
        return {
            "saved": True,
            "annotation": annotation.to_dict(),
            "annotation_path": str(page_path),
            "summary_json_path": str(self.summary_json_path) if self.summary_json_path else None,
        }

    def action(self, payload: dict) -> dict:
        image_id = int(payload["image_id"])
        annotation = self.parse_annotation(image_id, payload["annotation"])
        selected = set(payload.get("selected_ids") or [])
        settings = payload.get("settings") or {}
        view = {"orig_w": annotation.image_width, "orig_h": annotation.image_height,
                "orientation": annotation.orientation}
        action = payload["action"]
        if action == "group":
            mode = settings.get("mode", "vertical_columns")
            if mode not in ("vertical_columns", "horizontal_lines"):
                raise ValueError("Kiểu gom nhóm không hợp lệ.")
            annotation.regions = group_detections(
                annotation.raw_detections, mode=mode,
                min_score=float(settings.get("min_score", 0.25)),
                overlap_threshold=float(settings.get("overlap_threshold", 0.35)),
                reading_order=settings.get("reading_order", "rtl_ttb"), **view,
            )
            selected = set()
        elif action == "number":
            annotation.regions = bulk_number_regions(
                annotation.regions, selected,
                start_index=int(settings.get("start_index", 1)),
                order_mode=settings.get("reading_order", "rtl_ttb"),
                exclude_non_content=bool(settings.get("exclude_non_content", True)), **view,
            )
        elif action == "merge":
            annotation.regions, merged_id = merge_regions(annotation.regions, selected)
            selected = {merged_id} if merged_id else selected
        elif action == "split":
            if len(selected) != 1:
                raise ValueError("Chọn đúng một vùng để chia.")
            annotation.regions, parts = split_region(
                annotation.regions, next(iter(selected)), axis=payload["axis"],
                split_coord=payload.get("split_coord"),
            )
            selected = set(parts) if parts else selected
        else:
            raise ValueError("Thao tác không hợp lệ.")
        return {"annotation": annotation.to_dict(), "selected_ids": sorted(selected)}

    def start_detection(self, image_id: int, payload: dict) -> dict:
        entry = self.entry(image_id)
        orientation = normalize_orientation(int(payload.get("orientation", 0)))
        if not self.detection_lock.acquire(blocking=False):
            raise ValueError("Đang nhận diện một ảnh khác. Vui lòng đợi hoàn tất.")
        job_id = uuid.uuid4().hex
        self.jobs = {key: job for key, job in self.jobs.items() if job["status"] == "running"}
        self.jobs[job_id] = {"status": "running", "image_id": image_id}

        def work() -> None:
            try:
                self.detector.configure(
                    device=payload.get("device") or self.detector.device,
                    model_dir=Path(payload.get("model_dir") or self.detector.model_dir),
                )
                raw = self.detector.detect_image(entry.path, orientation=orientation)
                self.jobs[job_id] = {"status": "done", "image_id": image_id,
                                     "raw_detections": [d.to_dict() for d in raw]}
            except Exception as exc:
                self.jobs[job_id] = {"status": "error", "image_id": image_id, "error": str(exc)}
            finally:
                self.detection_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return {"job_id": job_id}

    def export(self, image_id: int, payload: dict) -> dict:
        annotation = self.parse_annotation(image_id, payload["annotation"])
        selected = payload.get("selected_ids")
        manifest = export_crops(
            self.entry(image_id), annotation, self.output_dir / "crops",
            selected_ids=set(selected) if selected is not None else None,
        )
        count = len(json.loads(manifest.read_text(encoding="utf-8"))["crops"])
        return {"manifest_path": str(manifest), "crop_count": count}


def make_handler(workspace: RegionWorkspace) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args) -> None:
            pass

        def send_bytes(self, content: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, payload: dict, status: int = 200) -> None:
            self.send_bytes(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                            "application/json; charset=utf-8", status)

        def read_json(self) -> dict:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Yêu cầu phải có Content-Type application/json.")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024 * 1024:
                raise ValueError("Dữ liệu yêu cầu rỗng hoặc quá lớn.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Yêu cầu JSON phải là một đối tượng.")
            return payload

        def dispatch(self) -> None:
            try:
                host = self.headers.get("Host", "")
                if urlsplit("http://" + host).hostname not in ("127.0.0.1", "localhost", "::1"):
                    self.send_json({"error": "Chỉ chấp nhận truy cập từ máy cục bộ."}, 403)
                    return
                if self.command != "GET":
                    origin = self.headers.get("Origin")
                    if (origin and origin != "http://" + host) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                        self.send_json({"error": "Không chấp nhận yêu cầu từ website khác."}, 403)
                        return
                path = urlsplit(self.path).path
                parts = path.strip("/").split("/")
                if self.command == "GET":
                    if path == "/api/bootstrap":
                        self.send_json(workspace.bootstrap())
                    elif len(parts) == 3 and parts[:2] == ["api", "pages"]:
                        image_id = int(parts[2])
                        annotation = workspace.load(image_id)
                        with workspace.save_lock:
                            save_session(workspace.output_dir, workspace.entry(image_id), image_id)
                        self.send_json(annotation.to_dict())
                    elif len(parts) == 3 and parts[:2] == ["api", "images"]:
                        image = workspace.entry(int(parts[2])).path
                        self.send_bytes(image.read_bytes(), mimetypes.guess_type(image.name)[0] or "image/png")
                    elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                        job = workspace.jobs.get(parts[2])
                        self.send_json(job or {"error": "Không tìm thấy tác vụ."}, 200 if job else 404)
                    elif path in ("/", "/index.html", "/app.js", "/style.css", "/favicon.svg"):
                        asset = STATIC_DIR / ("index.html" if path == "/" else path[1:])
                        self.send_bytes(asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or "text/plain")
                    else:
                        self.send_json({"error": "Không tìm thấy địa chỉ."}, 404)
                elif self.command == "PUT" and len(parts) == 3 and parts[:2] == ["api", "pages"]:
                    self.send_json(workspace.save(int(parts[2]), self.read_json()))
                elif self.command == "POST":
                    payload = self.read_json()
                    if path == "/api/action":
                        self.send_json(workspace.action(payload))
                    elif len(parts) == 3 and parts[:2] == ["api", "detect"]:
                        self.send_json(workspace.start_detection(int(parts[2]), payload), 202)
                    elif len(parts) == 3 and parts[:2] == ["api", "export"]:
                        self.send_json(workspace.export(int(parts[2]), payload))
                    else:
                        self.send_json({"error": "Không tìm thấy thao tác."}, 404)
                else:
                    self.send_json({"error": "Phương thức không hợp lệ."}, 405)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except FileNotFoundError:
                self.send_json({"error": "Không tìm thấy tệp được yêu cầu."}, 404)
            except json.JSONDecodeError as exc:
                self.send_json({"error": f"JSON bị lỗi; không ghi đè dữ liệu: {exc}"}, 409)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                self.send_json({"error": str(exc)}, 409 if self.command == "GET" else 400)
            except OSError as exc:
                self.send_json({"error": str(exc)}, 500)

        do_GET = dispatch
        do_PUT = dispatch
        do_POST = dispatch

    return Handler


def launch_server(workspace: RegionWorkspace, port: int = 8765, open_browser: bool = True) -> None:
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler(workspace)) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Sino Studio: {url} | {len(workspace.entries)} ảnh | {workspace.detector.device}", flush=True)
        if open_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nĐã dừng máy chủ.", flush=True)
