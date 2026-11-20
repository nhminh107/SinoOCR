"""HTTP server for reviewing OCR pages containing '□' and syncing edits to CSV + TXT."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import Image

STATIC_DIR = Path(__file__).resolve().parent / "static"
CSV_FILENAME = "nhat_ky_kiem_tra_ocr.csv"
BBOX_JSON_FILENAME = "square_bboxes.json"
CSV_FIELDS = [
    "tac_pham",
    "ngon_ngu",
    "trang",
    "dong",
    "so_dau_vuong",
    "text_ocr",
    "text_sua",
    "trang_thai",
    "ghi_chu",
]
VALID_STATUSES = ("CAN_XEM_LAI", "DA_SUA", "GIU_NGUYEN_MO")

_LOCK = threading.Lock()
_NUM_RE = re.compile(r"(\d+)")


def _page_sort_key(name: str) -> tuple[int, str]:
    m = _NUM_RE.search(name)
    return (int(m.group(1)) if m else 0, name)


def _atomic_write_text(target: Path, content: str, encoding: str = "utf-8") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=target.parent,
            prefix=".tmp_",
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


def _read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.is_file():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv_rows(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    tmp_path: Path | None = None
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=csv_path.parent,
            prefix=".tmp_csv_",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            writer = csv.DictWriter(tmp, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, csv_path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _read_bbox_json(json_path: Path) -> list[dict[str, Any]]:
    if not json_path.is_file():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _write_bbox_json(json_path: Path, tac_pham: str, items: list[dict[str, Any]]) -> None:
    payload = {
        "tac_pham": tac_pham,
        "total_bboxes": len(items),
        "items": items,
    }
    _atomic_write_text(
        json_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class SquareStore:
    def __init__(self, submission_dir: Path) -> None:
        self.submission_dir = submission_dir.resolve()
        self.project_root = self.submission_dir.parent
        self._manifest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self._log_lines_by_key: dict[tuple[str, str], dict[str, int]] = {}
        self._load_hint_sources()

    def _load_hint_sources(self) -> None:
        manifest_path = self.project_root / "outputs" / "packed_maxrects" / "manifest.json"
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                for page in data.get("pages", []):
                    key = (str(page.get("dataset", "")), str(page.get("filename", "")))
                    self._manifest_by_key[key] = page
            except Exception:
                pass

        log_path = self.project_root / "outputs" / "ocr_workflow_log.jsonl"
        if log_path.is_file():
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if obj.get("status") == "ok":
                        key = (str(obj.get("dataset", "")), str(obj.get("image", "")))
                        self._log_lines_by_key[key] = {
                            "han": int(obj.get("han_lines") or 0),
                            "viet": int(obj.get("viet_lines") or 0),
                        }
            except Exception:
                pass

    def _get_region_hints(
        self,
        tac_pham: str,
        ngon_ngu: str,
        trang: str,
        flagged_lines: list[int],
        total_lines: int,
    ) -> list[dict[str, Any]]:
        """Map original column regions from manifest.json onto merged page coordinates
        and estimate which column(s) contain the flagged lines."""
        merged_img = self.submission_dir / tac_pham / "01_nguon" / "pages" / f"{trang}.png"
        if not merged_img.is_file():
            return []

        with Image.open(merged_img) as im:
            merged_w = im.size[0]

        plain_entry = self._manifest_by_key.get((tac_pham, f"{trang}.png"))
        part1_entry = self._manifest_by_key.get((tac_pham, f"{trang}_1.png"))
        part2_entry = self._manifest_by_key.get((tac_pham, f"{trang}_2.png"))

        parts: list[tuple[str, dict[str, Any], int, int, int]] = []
        # tuple: (part_label, manifest_entry, x_offset, line_start_1based, line_end_1based)
        if plain_entry:
            parts.append(("full", plain_entry, 0, 1, max(1, total_lines)))
        else:
            p1_lines = self._log_lines_by_key.get((tac_pham, f"{trang}_1.png"), {}).get(ngon_ngu, 0)
            p2_lines = self._log_lines_by_key.get((tac_pham, f"{trang}_2.png"), {}).get(ngon_ngu, 0)
            if p1_lines == 0 and p2_lines == 0:
                p1_lines = total_lines // 2
                p2_lines = total_lines - p1_lines
            if part1_entry:
                parts.append(("_1", part1_entry, 0, 1, p1_lines))
            if part2_entry:
                p2_img = self.project_root / "data" / tac_pham / f"{trang}_2.png"
                offset_x2 = 0
                if p2_img.is_file():
                    with Image.open(p2_img) as im2:
                        offset_x2 = max(0, merged_w - im2.size[0])
                parts.append(("_2", part2_entry, offset_x2, p1_lines + 1, p1_lines + p2_lines))

        hints: list[dict[str, Any]] = []
        for part_label, entry, x_off, l_start, l_end in parts:
            regions = sorted(
                entry.get("regions", []),
                key=lambda r: int(r.get("reading_order", r.get("index", 0))),
            )
            n_reg = len(regions)
            if n_reg == 0:
                continue
            part_line_span = max(1, l_end - l_start + 1)

            # Determine which region indices are likely targets for flagged_lines in this part
            likely_reg_indices: dict[int, list[int]] = {}
            for fl in flagged_lines:
                if l_start <= fl <= l_end:
                    rel = (fl - l_start) / part_line_span
                    est_idx = min(n_reg - 1, max(0, int(rel * n_reg)))
                    likely_reg_indices.setdefault(est_idx, []).append(fl)

            for r_i, reg in enumerate(regions):
                ob = reg.get("orig_bbox")
                if not (isinstance(ob, list) and len(ob) == 4):
                    continue
                x1, y1, x2, y2 = [int(v) for v in ob]
                matched_lines = likely_reg_indices.get(r_i, [])
                # Mark immediate neighbors as secondary candidate so user sees a tight window
                is_primary = len(matched_lines) > 0
                is_neighbor = (not is_primary) and (
                    (r_i - 1) in likely_reg_indices or (r_i + 1) in likely_reg_indices
                )
                hints.append(
                    {
                        "part": part_label,
                        "region_index": int(reg.get("index", r_i + 1)),
                        "reading_order": int(reg.get("reading_order", r_i + 1)),
                        "label": str(reg.get("label", "body")),
                        "bbox": [x1 + x_off, y1, x2 + x_off, y2],
                        "is_primary": is_primary,
                        "is_neighbor": is_neighbor,
                        "matched_lines": matched_lines,
                    }
                )
        return hints

    def _datasets(self) -> list[Path]:
        if not self.submission_dir.is_dir():
            return []
        return sorted(
            p for p in self.submission_dir.iterdir()
            if p.is_dir() and (p / CSV_FILENAME).is_file()
        )

    def list_items(self) -> dict[str, Any]:
        """Group CSV entries by (dataset, lang, page) for page-level inspection."""
        groups: list[dict[str, Any]] = []
        total_rows = 0
        done_rows = 0
        for ds_dir in self._datasets():
            rows = _read_csv_rows(ds_dir / CSV_FILENAME)
            by_page: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for idx, row in enumerate(rows):
                key = (row["ngon_ngu"], row["trang"])
                by_page.setdefault(key, []).append({**row, "row_index": idx})
                total_rows += 1
                if row.get("trang_thai") in ("DA_SUA", "GIU_NGUYEN_MO"):
                    done_rows += 1

            all_bboxes = _read_bbox_json(ds_dir / BBOX_JSON_FILENAME)
            bbox_count_by_page: dict[tuple[str, str], int] = {}
            for b in all_bboxes:
                bkey = (str(b.get("ngon_ngu", "")), str(b.get("trang", "")))
                bbox_count_by_page[bkey] = bbox_count_by_page.get(bkey, 0) + 1

            for (lang, page), page_rows in sorted(
                by_page.items(), key=lambda kv: (kv[0][0], _page_sort_key(kv[0][1]))
            ):
                pending = sum(1 for r in page_rows if r.get("trang_thai") == "CAN_XEM_LAI")
                squares = sum(int(r.get("so_dau_vuong") or 0) for r in page_rows)
                groups.append(
                    {
                        "id": f"{ds_dir.name}/{lang}/{page}",
                        "tac_pham": ds_dir.name,
                        "ngon_ngu": lang,
                        "trang": page,
                        "line_count": len(page_rows),
                        "square_count": squares,
                        "pending_count": pending,
                        "bbox_count": bbox_count_by_page.get((lang, page), 0),
                    }
                )
        return {
            "pages": groups,
            "summary": {
                "total_pages": len(groups),
                "total_rows": total_rows,
                "done_rows": done_rows,
                "pending_rows": total_rows - done_rows,
            },
        }

    def get_page_detail(self, tac_pham: str, ngon_ngu: str, trang: str) -> dict[str, Any]:
        ds_dir = (self.submission_dir / tac_pham).resolve()
        if ds_dir.parent != self.submission_dir:
            raise ValueError("Đường dẫn tác phẩm không hợp lệ.")
        csv_path = ds_dir / CSV_FILENAME
        rows = _read_csv_rows(csv_path)
        matched = [
            {**r, "row_index": i}
            for i, r in enumerate(rows)
            if r["ngon_ngu"] == ngon_ngu and r["trang"] == trang
        ]

        txt_path = ds_dir / "02_ocr_tho" / ngon_ngu / f"{trang}.txt"
        clean_page_path = ds_dir / "03_text_sach" / ngon_ngu / f"{trang}.txt"
        source_txt = clean_page_path if clean_page_path.is_file() else txt_path
        full_lines = source_txt.read_text(encoding="utf-8").splitlines() if source_txt.is_file() else []

        # Attach 2 lines before/after as context for each flagged line
        for item in matched:
            ln = int(item["dong"])
            start = max(1, ln - 2)
            end = min(len(full_lines), ln + 2)
            item["context"] = [
                {"line_no": k, "text": full_lines[k - 1], "is_target": k == ln}
                for k in range(start, end + 1)
            ]

        img_path = ds_dir / "01_nguon" / "pages" / f"{trang}.png"
        all_bboxes = _read_bbox_json(ds_dir / BBOX_JSON_FILENAME)
        page_bboxes = [
            b for b in all_bboxes
            if b.get("ngon_ngu") == ngon_ngu and b.get("trang") == trang
        ]
        page_bboxes.sort(key=lambda b: int(b.get("error_index", 0)))

        flagged_lines = [int(r["dong"]) for r in matched]
        region_hints = self._get_region_hints(
            tac_pham=tac_pham,
            ngon_ngu=ngon_ngu,
            trang=trang,
            flagged_lines=flagged_lines,
            total_lines=len(full_lines),
        )

        return {
            "tac_pham": tac_pham,
            "ngon_ngu": ngon_ngu,
            "trang": trang,
            "has_image": img_path.is_file(),
            "image_path": str(img_path.relative_to(self.submission_dir.parent))
            if img_path.is_file()
            else "",
            "image_url": f"/api/image?tac_pham={tac_pham}&trang={trang}",
            "rows": matched,
            "bboxes": page_bboxes,
            "region_hints": region_hints,
            "full_text": "\n".join(full_lines),
        }

    def get_image_path(self, tac_pham: str, trang: str) -> Path:
        ds_dir = (self.submission_dir / tac_pham).resolve()
        if ds_dir.parent != self.submission_dir or not re.fullmatch(r"page\d+", trang):
            raise ValueError("Tham số ảnh không hợp lệ.")
        img_path = ds_dir / "01_nguon" / "pages" / f"{trang}.png"
        if not img_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy ảnh: {img_path}")
        return img_path

    def _rebuild_clean_merged(self, ds_dir: Path, ngon_ngu: str) -> Path:
        raw_dir = ds_dir / "02_ocr_tho" / ngon_ngu
        clean_dir = ds_dir / "03_text_sach"
        clean_lang_dir = clean_dir / ngon_ngu
        merged_blocks: list[str] = []

        if raw_dir.is_dir():
            for raw_file in sorted(raw_dir.glob("page*.txt"), key=lambda p: _page_sort_key(p.stem)):
                clean_page = clean_lang_dir / raw_file.name
                src = clean_page if clean_page.is_file() else raw_file
                text = src.read_text(encoding="utf-8").strip("\r\n")
                if text:
                    merged_blocks.append(text)

        merged_path = clean_dir / f"{ngon_ngu}.txt"
        content = ("\n".join(merged_blocks) + "\n") if merged_blocks else ""
        _atomic_write_text(merged_path, content, encoding="utf-8")
        return merged_path

    def save_updates(
        self,
        tac_pham: str,
        ngon_ngu: str,
        trang: str,
        updates: list[dict[str, Any]],
        bboxes: list[dict[str, Any]] | None = None,
        apply_to_txt: bool = True,
    ) -> dict[str, Any]:
        with _LOCK:
            ds_dir = (self.submission_dir / tac_pham).resolve()
            if ds_dir.parent != self.submission_dir:
                raise ValueError("Đường dẫn tác phẩm không hợp lệ.")
            csv_path = ds_dir / CSV_FILENAME
            rows = _read_csv_rows(csv_path)

            raw_txt_path = ds_dir / "02_ocr_tho" / ngon_ngu / f"{trang}.txt"
            clean_page_path = ds_dir / "03_text_sach" / ngon_ngu / f"{trang}.txt"
            src_txt = clean_page_path if clean_page_path.is_file() else raw_txt_path
            txt_lines = (
                src_txt.read_text(encoding="utf-8").splitlines()
                if src_txt.is_file()
                else []
            )

            row_by_index: dict[int, dict[str, str]] = {}
            for upd in updates:
                row_index = int(upd["row_index"])
                if not (0 <= row_index < len(rows)):
                    continue
                row = rows[row_index]
                if row["ngon_ngu"] != ngon_ngu or row["trang"] != trang:
                    continue

                new_text = str(upd.get("text_sua", row["text_sua"])).replace("\r", "").replace("\n", " ")
                status = str(upd.get("trang_thai", row["trang_thai"]))
                if status not in VALID_STATUSES:
                    status = "CAN_XEM_LAI"
                note = str(upd.get("ghi_chu", row.get("ghi_chu", ""))).strip()

                row["text_sua"] = new_text
                row["trang_thai"] = status
                row["ghi_chu"] = note
                row_by_index[row_index] = row

                if apply_to_txt:
                    line_no = int(row["dong"])
                    if 1 <= line_no <= len(txt_lines):
                        txt_lines[line_no - 1] = new_text

            _write_csv_rows(csv_path, rows)

            # Save per-page bboxes into submission/<tac_pham>/square_bboxes.json
            bbox_saved = False
            if bboxes is not None:
                bbox_json_path = ds_dir / BBOX_JSON_FILENAME
                existing = _read_bbox_json(bbox_json_path)
                kept = [
                    b for b in existing
                    if not (b.get("ngon_ngu") == ngon_ngu and b.get("trang") == trang)
                ]

                img_path = ds_dir / "01_nguon" / "pages" / f"{trang}.png"
                rel_img = str(img_path.relative_to(self.submission_dir.parent))
                clean_bboxes: list[dict[str, Any]] = []
                for idx, b in enumerate(bboxes, start=1):
                    coords = b.get("bbox", [])
                    if not (isinstance(coords, list) and len(coords) == 4):
                        continue
                    x1, y1, x2, y2 = [int(round(float(v))) for v in coords]
                    if x2 <= x1 or y2 <= y1:
                        continue
                    r_idx = b.get("row_index")
                    linked_row = row_by_index.get(int(r_idx)) if r_idx is not None else None
                    if linked_row is None and r_idx is not None and 0 <= int(r_idx) < len(rows):
                        linked_row = rows[int(r_idx)]
                    clean_bboxes.append(
                        {
                            "id": f"{tac_pham}_{ngon_ngu}_{trang}_err{idx}",
                            "tac_pham": tac_pham,
                            "ngon_ngu": ngon_ngu,
                            "trang": trang,
                            "image_file": f"{trang}.png",
                            "image_path": rel_img,
                            "error_index": idx,
                            "row_index": int(r_idx) if r_idx is not None else None,
                            "dong": int(linked_row["dong"]) if linked_row else b.get("dong"),
                            "text_ocr": linked_row["text_ocr"] if linked_row else str(b.get("text_ocr", "")),
                            "text_sua": linked_row["text_sua"] if linked_row else str(b.get("text_sua", "")),
                            "bbox": [x1, y1, x2, y2],
                            "note": str(b.get("note", "")).strip(),
                        }
                    )

                merged_bboxes = kept + clean_bboxes
                merged_bboxes.sort(
                    key=lambda item: (
                        item.get("ngon_ngu", ""),
                        _page_sort_key(str(item.get("trang", ""))),
                        int(item.get("error_index", 0)),
                    )
                )
                _write_bbox_json(bbox_json_path, tac_pham, merged_bboxes)
                bbox_saved = True

            clean_updated = False
            if apply_to_txt and raw_txt_path.is_file():
                _atomic_write_text(clean_page_path, "\n".join(txt_lines) + "\n", encoding="utf-8")
                self._rebuild_clean_merged(ds_dir, ngon_ngu)
                clean_updated = True

            return {
                "ok": True,
                "txt_updated": clean_updated,
                "bbox_saved": bbox_saved,
                "detail": self.get_page_detail(tac_pham, ngon_ngu, trang),
                "list": self.list_items(),
            }


def make_handler(store: SquareStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/static/style.css":
                self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
                return
            if path == "/static/app.js":
                self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
                return

            if path == "/api/pages":
                self._send_json(store.list_items())
                return

            if path == "/api/page":
                try:
                    tac_pham = qs["tac_pham"][0]
                    ngon_ngu = qs["ngon_ngu"][0]
                    trang = qs["trang"][0]
                    self._send_json(store.get_page_detail(tac_pham, ngon_ngu, trang))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/image":
                try:
                    tac_pham = qs["tac_pham"][0]
                    trang = qs["trang"][0]
                    img_path = store.get_image_path(tac_pham, trang)
                    self._send_file(img_path, "image/png")
                except Exception as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = store.save_updates(
                    tac_pham=payload["tac_pham"],
                    ngon_ngu=payload["ngon_ngu"],
                    trang=payload["trang"],
                    updates=payload.get("updates", []),
                    bboxes=payload.get("bboxes"),
                    apply_to_txt=bool(payload.get("apply_to_txt", True)),
                )
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def run_server(submission_dir: Path, host: str = "127.0.0.1", port: int = 8768) -> None:
    store = SquareStore(submission_dir)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
