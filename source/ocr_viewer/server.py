"""HTTP server, dataset index, TXT writer, and sidecar review store for the local OCR viewer."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "static"

STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

DATASET_LABELS: dict[str, str] = {
    "01_QuocTrieuHuongKhoaLuc": "01 - Quốc triều hương khoa lục",
    "02_TuLiemDaiKhoaLuc": "02 - Từ Liêm đại khoa lục",
}

VALID_REVIEW_STATUSES = ("unreviewed", "needs_review", "approved")
MAX_JSON_BODY_BYTES = 4 * 1024 * 1024

_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_TEXT_WRITE_LOCK = threading.Lock()


class ReviewStoreError(RuntimeError):
    """Raised when the sidecar review JSON file cannot be safely read or written."""


class TextWriteError(RuntimeError):
    """Raised when an OCR TXT file cannot be safely written."""


def natural_sort_key(name: str) -> list[tuple[int, int | str]]:
    """Sort key that orders numbers naturally (e.g. page2 before page10)."""
    parts = _NATURAL_SPLIT_RE.split(name)
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def _atomic_write_utf8(target: Path, content: str, prefix: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=prefix,
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


class DatasetIndex:
    """In-memory index of datasets and server-controlled image IDs."""

    def __init__(self, data_dir: Path, submission_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.submission_dir = submission_dir.resolve()
        self.datasets: dict[str, dict[str, Any]] = {}
        self.dataset_order: list[str] = []
        self._build_index()

    def _build_index(self) -> None:
        if not self.data_dir.is_dir():
            return

        candidate_dirs = sorted(
            (p for p in self.data_dir.iterdir() if p.is_dir()),
            key=lambda p: natural_sort_key(p.name),
        )

        for dataset_dir in candidate_dirs:
            png_files = sorted(
                (p for p in dataset_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"),
                key=lambda p: natural_sort_key(p.name),
            )
            if not png_files:
                continue

            dataset_id = dataset_dir.name
            han_dir = self.submission_dir / dataset_id / "02_ocr_tho" / "han"
            viet_dir = self.submission_dir / dataset_id / "02_ocr_tho" / "viet"

            items: list[dict[str, Any]] = []
            by_id: dict[str, dict[str, Any]] = {}

            for png_path in png_files:
                stem = png_path.stem
                han_path = han_dir / f"{stem}.txt"
                viet_path = viet_dir / f"{stem}.txt"
                item = {
                    "id": stem,
                    "filename": png_path.name,
                    "review_key": f"data/{dataset_id}/{png_path.name}",
                    "image_path": png_path,
                    "han_path": han_path,
                    "viet_path": viet_path,
                    "has_han": han_path.is_file(),
                    "has_viet": viet_path.is_file(),
                }
                items.append(item)
                by_id[stem] = item

            self.dataset_order.append(dataset_id)
            self.datasets[dataset_id] = {
                "id": dataset_id,
                "label": DATASET_LABELS.get(dataset_id, dataset_id),
                "items": items,
                "by_id": by_id,
            }

    def list_datasets(self) -> list[dict[str, Any]]:
        return [
            {
                "id": ds_id,
                "label": self.datasets[ds_id]["label"],
                "count": len(self.datasets[ds_id]["items"]),
            }
            for ds_id in self.dataset_order
        ]

    def list_images(self, dataset_id: str) -> list[dict[str, Any]] | None:
        dataset = self.datasets.get(dataset_id)
        if dataset is None:
            return None
        return [
            {
                "id": item["id"],
                "filename": item["filename"],
                "has_han": item["has_han"],
                "has_viet": item["has_viet"],
            }
            for item in dataset["items"]
        ]

    def get_item(self, dataset_id: str, image_id: str) -> dict[str, Any] | None:
        dataset = self.datasets.get(dataset_id)
        if dataset is None:
            return None
        return dataset["by_id"].get(image_id)


class ReviewStore:
    """Thread-safe, atomic sidecar JSON store for per-image review status and notes."""

    def __init__(self, review_file: Path) -> None:
        self.review_file = review_file.resolve()
        self._lock = threading.Lock()

    def _load_locked(self) -> dict[str, dict[str, str]]:
        if not self.review_file.exists():
            return {}
        try:
            raw = self.review_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewStoreError(
                f"File review bị hỏng hoặc không đọc được ({self.review_file.name}); không ghi đè."
            ) from exc

        if not isinstance(parsed, dict):
            raise ReviewStoreError(
                f"Cấu trúc file review không hợp lệ ({self.review_file.name}); không ghi đè."
            )
        return parsed

    def _write_locked(self, data: dict[str, dict[str, str]]) -> None:
        try:
            serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            _atomic_write_utf8(self.review_file, serialized, ".ocr_review_")
        except Exception as exc:
            raise ReviewStoreError(f"Không thể ghi file review: {exc}") from exc

    def _normalize_entry(self, entry: Any) -> dict[str, str]:
        if not isinstance(entry, dict):
            return {"status": "unreviewed", "note": "", "updated_at": ""}
        status = entry.get("status")
        note = entry.get("note")
        updated_at = entry.get("updated_at")
        return {
            "status": status if status in VALID_REVIEW_STATUSES else "unreviewed",
            "note": note if isinstance(note, str) else "",
            "updated_at": updated_at if isinstance(updated_at, str) else "",
        }

    def get_review(self, image_key: str) -> dict[str, str]:
        with self._lock:
            data = self._load_locked()
            return self._normalize_entry(data.get(image_key))

    def mark_approved_if_unreviewed(self, image_key: str) -> dict[str, str]:
        """Auto-mark an image as approved when opened if it has no status or is unreviewed."""
        with self._lock:
            data = self._load_locked()
            current = self._normalize_entry(data.get(image_key))
            if current["status"] != "unreviewed":
                return current
            entry = {
                "status": "approved",
                "note": current["note"],
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            data[image_key] = entry
            self._write_locked(data)
            return entry

    def save_review(self, image_key: str, status: str, note: str) -> dict[str, str]:
        if status not in VALID_REVIEW_STATUSES:
            raise ValueError("Trạng thái duyệt không hợp lệ.")
        if not isinstance(note, str):
            raise ValueError("Ghi chú phải là chuỗi văn bản.")

        with self._lock:
            data = self._load_locked()
            entry = {
                "status": status,
                "note": note,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            data[image_key] = entry
            self._write_locked(data)
            return entry


def _read_text_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "text": ""}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"exists": True, "text": text}


def _write_text_file(path: Path, text: str) -> None:
    with _TEXT_WRITE_LOCK:
        try:
            _atomic_write_utf8(path, text, ".ocr_txt_")
        except Exception as exc:
            raise TextWriteError(f"Không thể ghi file TXT ({path.name}): {exc}") from exc


class OCRViewerHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server holding the read-only dataset index, TXT writer, and review store."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        index: DatasetIndex,
        review_store: ReviewStore,
    ) -> None:
        self.index = index
        self.review_store = review_store
        super().__init__(server_address, OCRViewerRequestHandler)


class OCRViewerRequestHandler(BaseHTTPRequestHandler):
    """Request handler for static assets, OCR/image APIs, and sidecar review API."""

    server: OCRViewerHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route in STATIC_FILES:
            filename, content_type = STATIC_FILES[route]
            self._serve_static(filename, content_type)
            return

        if route == "/api/datasets":
            self._send_json(HTTPStatus.OK, {"datasets": self.server.index.list_datasets()})
            return

        if route == "/api/images":
            params = parse_qs(parsed.query, keep_blank_values=False)
            dataset_id = _first_param(params, "dataset")
            if not dataset_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset."})
                return
            images = self.server.index.list_images(dataset_id)
            if images is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy tác phẩm."})
                return
            self._send_json(HTTPStatus.OK, {"dataset": dataset_id, "images": images})
            return

        if route == "/api/ocr":
            item, error_status, error_msg = self._resolve_item_from_query(parsed.query)
            if item is None:
                self._send_json(error_status, {"error": error_msg})
                return

            han_data = _read_text_file(item["han_path"])
            viet_data = _read_text_file(item["viet_path"])

            review_error: str | None = None
            try:
                review = self.server.review_store.mark_approved_if_unreviewed(item["review_key"])
            except ReviewStoreError as exc:
                review = {"status": "unreviewed", "note": "", "updated_at": ""}
                review_error = str(exc)

            payload: dict[str, Any] = {
                "dataset": _first_param(parse_qs(parsed.query), "dataset"),
                "id": item["id"],
                "filename": item["filename"],
                "review_key": item["review_key"],
                "han": han_data,
                "viet": viet_data,
                "review": review,
            }
            if review_error is not None:
                payload["review_error"] = review_error
            self._send_json(HTTPStatus.OK, payload)
            return

        if route == "/api/image":
            item, error_status, error_msg = self._resolve_item_from_query(parsed.query)
            if item is None:
                self._send_json(error_status, {"error": error_msg})
                return
            self._serve_png(item["image_path"])
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/review":
            self._handle_save_review()
            return
        if parsed.path == "/api/save":
            self._handle_save_all()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})

    def _handle_save_review(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        item = self._validate_item_from_body(body)
        if item is None:
            return

        status = body.get("status")
        note = body.get("note")
        if not isinstance(status, str) or status not in VALID_REVIEW_STATUSES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Trạng thái duyệt không hợp lệ."})
            return
        if not isinstance(note, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Ghi chú phải là chuỗi văn bản."})
            return

        try:
            entry = self.server.review_store.save_review(item["review_key"], status, note)
        except ReviewStoreError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "review_key": item["review_key"], "review": entry},
        )

    def _handle_save_all(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        item = self._validate_item_from_body(body)
        if item is None:
            return

        status = body.get("status")
        note = body.get("note")
        han_text = body.get("han_text")
        viet_text = body.get("viet_text")

        if not isinstance(status, str) or status not in VALID_REVIEW_STATUSES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Trạng thái duyệt không hợp lệ."})
            return
        if not isinstance(note, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Ghi chú phải là chuỗi văn bản."})
            return
        if han_text is not None and not isinstance(han_text, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Nội dung Hán–Nôm phải là chuỗi văn bản."})
            return
        if viet_text is not None and not isinstance(viet_text, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Nội dung Việt phải là chuỗi văn bản."})
            return

        try:
            if han_text is not None:
                _write_text_file(item["han_path"], han_text)
                item["has_han"] = True
            if viet_text is not None:
                _write_text_file(item["viet_path"], viet_text)
                item["has_viet"] = True
            entry = self.server.review_store.save_review(item["review_key"], status, note)
        except TextWriteError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        except ReviewStoreError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "review_key": item["review_key"],
                "review": entry,
                "han_exists": item["has_han"],
                "viet_exists": item["has_viet"],
            },
        )

    def _validate_item_from_body(self, body: dict[str, Any]) -> dict[str, Any] | None:
        dataset_id = body.get("dataset")
        image_id = body.get("id")
        if not isinstance(dataset_id, str) or not isinstance(image_id, str) or not dataset_id or not image_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset hoặc id."})
            return None
        item = self.server.index.get_item(dataset_id, image_id)
        if item is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "ID ảnh hoặc tác phẩm không hợp lệ."})
            return None
        return item

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu Content-Length."})
            return None
        try:
            content_length = int(raw_len)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Content-Length không hợp lệ."})
            return None
        if content_length <= 0 or content_length > MAX_JSON_BODY_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Kích thước dữ liệu không hợp lệ."})
            return None

        raw_body = self.rfile.read(content_length)
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON gửi lên không hợp lệ."})
            return None
        if not isinstance(parsed, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Payload phải là JSON object."})
            return None
        return parsed

    def _resolve_item_from_query(self, query: str) -> tuple[dict[str, Any] | None, HTTPStatus, str]:
        params = parse_qs(query, keep_blank_values=False)
        dataset_id = _first_param(params, "dataset")
        image_id = _first_param(params, "id")
        if not dataset_id or not image_id:
            return None, HTTPStatus.BAD_REQUEST, "Thiếu tham số dataset hoặc id."
        item = self.server.index.get_item(dataset_id, image_id)
        if item is None:
            return None, HTTPStatus.NOT_FOUND, "ID ảnh hoặc tác phẩm không hợp lệ."
        return item, HTTPStatus.OK, ""

    def _serve_static(self, filename: str, content_type: str) -> None:
        file_path = STATIC_DIR / filename
        if not file_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy tệp tĩnh."})
            return
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_png(self, image_path: Path) -> None:
        if not image_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy file ảnh."})
            return
        data = image_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _first_param(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    return values[0]


def create_server(
    host: str = "127.0.0.1",
    port: int = 8767,
    data_dir: Path | str = "data",
    submission_dir: Path | str = "submission",
    review_file: Path | str = "outputs/ocr_review.json",
) -> OCRViewerHTTPServer:
    """Create the localhost OCR viewer server."""
    index = DatasetIndex(Path(data_dir), Path(submission_dir))
    review_store = ReviewStore(Path(review_file))
    return OCRViewerHTTPServer((host, port), index, review_store)
