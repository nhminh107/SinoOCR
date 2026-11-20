"""HTTP server and atomic file writer for the per-page 3-column submission proofreading UI."""

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
}

DATASET_LABELS: dict[str, str] = {
    "01_QuocTrieuHuongKhoaLuc": "01 - Quốc triều hương khoa lục",
    "02_TuLiemDaiKhoaLuc": "02 - Từ Liêm đại khoa lục",
}

IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

MAX_JSON_BODY_BYTES = 16 * 1024 * 1024
NOTE_FILENAME = "nhat_ky_kiem_tra_han_viet.txt"
_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_NOTE_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_WRITE_LOCK = threading.Lock()


def natural_sort_key(name: str) -> list[tuple[int, int | str]]:
    """Sort naturally so page2 comes before page10."""
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


def atomic_write_utf8(target: Path, content: str) -> None:
    """Atomically write UTF-8 text to target using temp file + fsync + replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
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


def read_page_text(ds: dict[str, Any], page_id: str, lang: str) -> dict[str, Any]:
    """Read per-page text from 03_text_sach/<lang>/ first, falling back to 02_ocr_tho/<lang>/."""
    ds_dir: Path = ds["dir"]
    dataset_id: str = ds["id"]
    path_03 = ds_dir / "03_text_sach" / lang / f"{page_id}.txt"
    path_02 = ds_dir / "02_ocr_tho" / lang / f"{page_id}.txt"
    rel_03 = f"submission/{dataset_id}/03_text_sach/{lang}/{page_id}.txt"
    rel_02 = f"submission/{dataset_id}/02_ocr_tho/{lang}/{page_id}.txt"

    if path_03.is_file():
        return {
            "text": path_03.read_text(encoding="utf-8", errors="replace"),
            "source": "03_text_sach",
            "source_path": rel_03,
            "target_path": rel_03,
        }
    if path_02.is_file():
        return {
            "text": path_02.read_text(encoding="utf-8", errors="replace"),
            "source": "02_ocr_tho",
            "source_path": rel_02,
            "target_path": rel_03,
        }
    return {
        "text": "",
        "source": "missing",
        "source_path": "",
        "target_path": rel_03,
    }


def rebuild_merged_txt(ds: dict[str, Any], lang: str) -> None:
    """Rebuild 03_text_sach/<lang>.txt by merging all pages (03 override else 02)."""
    ds_dir: Path = ds["dir"]
    parts: list[str] = []
    for page in ds["pages"]:
        page_id = page["id"]
        path_03 = ds_dir / "03_text_sach" / lang / f"{page_id}.txt"
        path_02 = ds_dir / "02_ocr_tho" / lang / f"{page_id}.txt"
        src = path_03 if path_03.is_file() else path_02
        if not src.is_file():
            continue
        txt = src.read_text(encoding="utf-8", errors="replace").strip()
        if txt:
            parts.append(txt)
    merged = ("\n".join(parts) + "\n") if parts else ""
    atomic_write_utf8(ds_dir / "03_text_sach" / f"{lang}.txt", merged)


def read_notes_file(ds_dir: Path) -> dict[str, str]:
    """Parse per-page notes from nhat_ky_kiem_tra_han_viet.txt."""
    note_path = ds_dir / NOTE_FILENAME
    if not note_path.is_file():
        return {}
    raw = note_path.read_text(encoding="utf-8", errors="replace")
    notes: dict[str, str] = {}
    current_page: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_page is None:
            return
        text = "\n".join(buffer).strip()
        if text:
            notes[current_page] = text

    for line in raw.splitlines():
        match = _NOTE_HEADER_RE.match(line)
        if match:
            flush()
            current_page = match.group(1).strip()
            buffer = []
        elif current_page is not None:
            buffer.append(line)
    flush()
    return notes


def write_notes_file(ds_dir: Path, notes: dict[str, str]) -> None:
    """Write per-page notes atomically to nhat_ky_kiem_tra_han_viet.txt in natural page order."""
    blocks: list[str] = []
    for page_id in sorted(notes.keys(), key=natural_sort_key):
        text = notes[page_id].strip()
        if text:
            blocks.append(f"[{page_id}]\n{text}")
    content = ("\n\n".join(blocks) + "\n") if blocks else ""
    atomic_write_utf8(ds_dir / NOTE_FILENAME, content)


class ProofreadIndex:
    """Server-side index of datasets in submission/ with per-page images and txt paths."""

    def __init__(self, submission_dir: Path) -> None:
        self.submission_dir = submission_dir.resolve()
        self.datasets: dict[str, dict[str, Any]] = {}
        self.dataset_order: list[str] = []
        self._build_index()

    def _build_index(self) -> None:
        if not self.submission_dir.is_dir():
            return

        candidate_dirs = sorted(
            (p for p in self.submission_dir.iterdir() if p.is_dir()),
            key=lambda p: natural_sort_key(p.name),
        )

        for ds_dir in candidate_dirs:
            pages_dir = ds_dir / "01_nguon" / "pages"
            if not pages_dir.is_dir():
                continue

            image_files = sorted(
                (
                    p
                    for p in pages_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_MIME_TYPES
                ),
                key=lambda p: natural_sort_key(p.name),
            )
            if not image_files:
                continue

            pages: list[dict[str, Any]] = []
            pages_by_id: dict[str, dict[str, Any]] = {}
            for img_path in image_files:
                page_info = {
                    "id": img_path.stem,
                    "filename": img_path.name,
                    "path": img_path,
                    "mime": IMAGE_MIME_TYPES[img_path.suffix.lower()],
                }
                pages.append(page_info)
                pages_by_id[img_path.stem] = page_info

            dataset_id = ds_dir.name
            self.dataset_order.append(dataset_id)
            self.datasets[dataset_id] = {
                "id": dataset_id,
                "label": DATASET_LABELS.get(dataset_id, dataset_id),
                "dir": ds_dir,
                "pages": pages,
                "pages_by_id": pages_by_id,
            }

    def list_datasets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for ds_id in self.dataset_order:
            ds = self.datasets[ds_id]
            ds_dir: Path = ds["dir"]
            han_03 = sum(
                1 for p in ds["pages"] if (ds_dir / "03_text_sach" / "han" / f"{p['id']}.txt").is_file()
            )
            viet_03 = sum(
                1 for p in ds["pages"] if (ds_dir / "03_text_sach" / "viet" / f"{p['id']}.txt").is_file()
            )
            result.append(
                {
                    "id": ds["id"],
                    "label": ds["label"],
                    "page_count": len(ds["pages"]),
                    "han_03_count": han_03,
                    "viet_03_count": viet_03,
                }
            )
        return result

    def list_pages(self, dataset_id: str) -> list[dict[str, Any]] | None:
        ds = self.datasets.get(dataset_id)
        if ds is None:
            return None
        ds_dir: Path = ds["dir"]
        notes = read_notes_file(ds_dir)
        return [
            {
                "id": p["id"],
                "filename": p["filename"],
                "has_han_03": (ds_dir / "03_text_sach" / "han" / f"{p['id']}.txt").is_file(),
                "has_viet_03": (ds_dir / "03_text_sach" / "viet" / f"{p['id']}.txt").is_file(),
                "has_note": p["id"] in notes,
            }
            for p in ds["pages"]
        ]

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        return self.datasets.get(dataset_id)

    def get_page(self, dataset_id: str, page_id: str) -> dict[str, Any] | None:
        ds = self.datasets.get(dataset_id)
        if ds is None:
            return None
        return ds["pages_by_id"].get(page_id)


class ProofreadHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], index: ProofreadIndex) -> None:
        self.index = index
        super().__init__(server_address, ProofreadRequestHandler)


class ProofreadRequestHandler(BaseHTTPRequestHandler):
    server: ProofreadHTTPServer

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

        if route == "/api/pages":
            params = parse_qs(parsed.query, keep_blank_values=False)
            dataset_id = _first_param(params, "dataset")
            if not dataset_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset."})
                return
            ds = self.server.index.get_dataset(dataset_id)
            pages = self.server.index.list_pages(dataset_id)
            if ds is None or pages is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy tác phẩm."})
                return
            self._send_json(
                HTTPStatus.OK,
                {"dataset": ds["id"], "label": ds["label"], "pages": pages},
            )
            return

        if route == "/api/page":
            params = parse_qs(parsed.query, keep_blank_values=False)
            dataset_id = _first_param(params, "dataset")
            page_id = _first_param(params, "page")
            if not dataset_id or not page_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset hoặc page."})
                return
            ds = self.server.index.get_dataset(dataset_id)
            page = self.server.index.get_page(dataset_id, page_id)
            if ds is None or page is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy trang ảnh."})
                return
            notes = read_notes_file(ds["dir"])
            self._send_json(
                HTTPStatus.OK,
                {
                    "dataset": ds["id"],
                    "page": page["id"],
                    "filename": page["filename"],
                    "han": read_page_text(ds, page["id"], "han"),
                    "viet": read_page_text(ds, page["id"], "viet"),
                    "note": notes.get(page["id"], ""),
                    "note_path": f"submission/{ds['id']}/{NOTE_FILENAME}",
                },
            )
            return

        if route == "/api/image":
            params = parse_qs(parsed.query, keep_blank_values=False)
            dataset_id = _first_param(params, "dataset")
            page_id = _first_param(params, "page")
            if not dataset_id or not page_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset hoặc page."})
                return
            page = self.server.index.get_page(dataset_id, page_id)
            if page is None or not page["path"].is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy ảnh trang."})
                return
            data = page["path"].read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", page["mime"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/save":
            self._handle_save()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})

    def _handle_save(self) -> None:
        body = self._read_json_body()
        if body is None:
            return

        dataset_id = body.get("dataset")
        page_id = body.get("page")
        if not isinstance(dataset_id, str) or not isinstance(page_id, str) or not dataset_id or not page_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Thiếu tham số dataset hoặc page."})
            return

        ds = self.server.index.get_dataset(dataset_id)
        page = self.server.index.get_page(dataset_id, page_id)
        if ds is None or page is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy tác phẩm hoặc trang ảnh."})
            return

        han_text = body.get("han_text")
        viet_text = body.get("viet_text")
        note_text = body.get("note")

        if han_text is None and viet_text is None and note_text is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Không có nội dung nào để lưu."})
            return
        if han_text is not None and not isinstance(han_text, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "han_text phải là chuỗi văn bản."})
            return
        if viet_text is not None and not isinstance(viet_text, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "viet_text phải là chuỗi văn bản."})
            return
        if note_text is not None and not isinstance(note_text, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "note phải là chuỗi văn bản."})
            return

        saved: list[str] = []
        synced_merged: list[str] = []
        ds_dir: Path = ds["dir"]
        try:
            with _WRITE_LOCK:
                if han_text is not None:
                    target_han = ds_dir / "03_text_sach" / "han" / f"{page_id}.txt"
                    atomic_write_utf8(target_han, han_text)
                    rebuild_merged_txt(ds, "han")
                    saved.append(f"03_text_sach/han/{page_id}.txt")
                    synced_merged.append("03_text_sach/han.txt")
                if viet_text is not None:
                    target_viet = ds_dir / "03_text_sach" / "viet" / f"{page_id}.txt"
                    atomic_write_utf8(target_viet, viet_text)
                    rebuild_merged_txt(ds, "viet")
                    saved.append(f"03_text_sach/viet/{page_id}.txt")
                    synced_merged.append("03_text_sach/viet.txt")
                if note_text is not None:
                    notes = read_notes_file(ds_dir)
                    cleaned_note = note_text.strip()
                    if cleaned_note:
                        notes[page_id] = cleaned_note
                    else:
                        notes.pop(page_id, None)
                    write_notes_file(ds_dir, notes)
                    saved.append(NOTE_FILENAME)
        except OSError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Lỗi khi ghi file: {exc}"})
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "dataset": dataset_id,
                "page": page_id,
                "saved": saved,
                "synced_merged": synced_merged,
                "saved_at": datetime.now().astimezone().strftime("%H:%M:%S"),
            },
        )

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
    return values[0] if values else None


def create_server(
    host: str = "127.0.0.1",
    port: int = 8768,
    submission_dir: Path | str = "submission",
) -> ProofreadHTTPServer:
    """Create the localhost per-page 3-column proofreading server."""
    index = ProofreadIndex(Path(submission_dir))
    return ProofreadHTTPServer((host, port), index)
