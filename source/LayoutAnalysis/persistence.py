"""Lưu/tải annotation JSON theo từng ảnh, session khôi phục và xuất ảnh crop."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from PIL import Image

from .geometry import (
    NON_CONTENT_LABELS,
    PROJECT_DIR,
    ImageEntry,
    RawDetection,
    Region,
    clamp_bbox,
    normalize_orientation,
    normalize_reading_orders,
)

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


@dataclass
class PageAnnotation:
    dataset: str
    filename: str
    image_path: str
    image_width: int
    image_height: int
    orientation: int = 0
    page_index: int | None = None
    image_part: int | None = None
    pdf_basename: str | None = None
    pdf_page_range: list[int] | None = None
    pdf_page_index: int | None = None
    page_review_state: str = "pending"
    page_notes: str = ""
    updated_at: str = field(default_factory=_utc_now_iso)
    raw_detections: list[RawDetection] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "dataset": self.dataset,
            "filename": self.filename,
            "image_path": self.image_path,
            "image_width": int(self.image_width),
            "image_height": int(self.image_height),
            "orientation": normalize_orientation(self.orientation),
            "source": {
                "page_index": self.page_index,
                "image_part": self.image_part,
                "pdf_basename": self.pdf_basename,
                "pdf_page_range": self.pdf_page_range,
                "pdf_page_index": self.pdf_page_index,
            },
            "page_review_state": self.page_review_state,
            "page_notes": self.page_notes,
            "updated_at": self.updated_at,
            "raw_detections": [d.to_dict() for d in self.raw_detections],
            "regions": [r.to_dict() for r in normalize_reading_orders(self.regions)],
        }

    @classmethod
    def from_dict(cls, data: dict) -> PageAnnotation:
        source = data.get("source") or {}
        raw_list = [RawDetection.from_dict(item) for item in data.get("raw_detections", [])]
        reg_list = [Region.from_dict(item) for item in data.get("regions", [])]
        return cls(
            dataset=str(data["dataset"]),
            filename=str(data["filename"]),
            image_path=str(data.get("image_path", "")),
            image_width=int(data["image_width"]),
            image_height=int(data["image_height"]),
            orientation=normalize_orientation(int(data.get("orientation", 0))),
            page_index=source.get("page_index"),
            image_part=source.get("image_part"),
            pdf_basename=source.get("pdf_basename"),
            pdf_page_range=source.get("pdf_page_range"),
            pdf_page_index=source.get("pdf_page_index"),
            page_review_state=str(data.get("page_review_state", "pending")),
            page_notes=str(data.get("page_notes", "")),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            raw_detections=raw_list,
            regions=normalize_reading_orders(reg_list),
        )


def annotation_path_for(output_dir: Path, entry: ImageEntry) -> Path:
    return output_dir / entry.dataset / f"{entry.stem}.json"


def new_annotation_for_entry(entry: ImageEntry, width: int, height: int) -> PageAnnotation:
    pdf_range = None
    pdf_basename = None
    if entry.pdf_source is not None:
        pdf_basename = entry.pdf_source.pdf_basename
        pdf_range = [entry.pdf_source.pdf_page_start, entry.pdf_source.pdf_page_end]
    return PageAnnotation(
        dataset=entry.dataset,
        filename=entry.filename,
        image_path=str(entry.path),
        image_width=width,
        image_height=height,
        orientation=0,
        page_index=entry.page_index,
        image_part=entry.image_part,
        pdf_basename=pdf_basename,
        pdf_page_range=pdf_range,
        pdf_page_index=entry.pdf_page_index,
    )


def save_annotation(output_dir: Path, entry: ImageEntry, annotation: PageAnnotation) -> Path:
    annotation.updated_at = _utc_now_iso()
    path = annotation_path_for(output_dir, entry)
    _atomic_write_json(path, annotation.to_dict())
    return path


def _entry_relative_path(entry: ImageEntry) -> str:
    try:
        return entry.path.resolve().relative_to(PROJECT_DIR.resolve()).as_posix()
    except ValueError:
        return f"{entry.dataset}/{entry.filename}"


def update_summary_json(summary_path: Path, entry: ImageEntry, annotation: PageAnnotation) -> Path:
    """Cập nhật trực tiếp trang vừa chỉnh sửa vào file JSON tổng (VD: results/layout_all.json)."""
    if summary_path.is_file():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
            payload = {"schema_version": SCHEMA_VERSION, "pages": []}
    else:
        payload = {"schema_version": SCHEMA_VERSION, "pages": []}

    pages: list[dict] = payload["pages"]
    rel_path = _entry_relative_path(entry)
    ordered_regions = normalize_reading_orders(annotation.regions)
    directions = {r.direction for r in ordered_regions}
    script_dir = "horizontal" if directions == {"horizontal"} else "vertical"

    new_bboxes = [
        {
            "id": r.id,
            "bbox": [round(float(v), 2) for v in r.bbox],
            "polygon": [
                [round(float(r.bbox[0]), 2), round(float(r.bbox[1]), 2)],
                [round(float(r.bbox[2]), 2), round(float(r.bbox[1]), 2)],
                [round(float(r.bbox[2]), 2), round(float(r.bbox[3]), 2)],
                [round(float(r.bbox[0]), 2), round(float(r.bbox[3]), 2)],
            ],
            "label": r.label,
            "position": int(r.reading_order),
            "reading_order": int(r.reading_order),
            "direction": r.direction,
            "language": r.language,
            "confidence": 1.0,
        }
        for r in ordered_regions
    ]

    target_idx = next(
        (
            i
            for i, p in enumerate(pages)
            if p.get("relative_path") == rel_path
            or (p.get("dataset") == entry.dataset and p.get("filename") == entry.filename)
        ),
        None,
    )

    existing_page = dict(pages[target_idx]) if target_idx is not None else {}
    existing_page.update(
        {
            "relative_path": rel_path,
            "dataset": entry.dataset,
            "filename": entry.filename,
            "image_width": int(annotation.image_width),
            "image_height": int(annotation.image_height),
            "image_bbox": [0.0, 0.0, float(annotation.image_width), float(annotation.image_height)],
            "orientation": normalize_orientation(annotation.orientation),
            "script_direction": script_dir,
            "num_regions": len(ordered_regions),
            "num_titles": sum(1 for r in ordered_regions if r.label == "title"),
            "page_review_state": annotation.page_review_state,
            "bboxes": new_bboxes,
            "regions": [r.to_dict() for r in ordered_regions],
            "source": {
                "page_index": annotation.page_index,
                "image_part": annotation.image_part,
                "pdf_basename": annotation.pdf_basename,
                "pdf_page_index": annotation.pdf_page_index,
            },
            "updated_at": annotation.updated_at,
        }
    )

    if target_idx is not None:
        pages[target_idx] = existing_page
    else:
        pages.append(existing_page)

    payload["schema_version"] = SCHEMA_VERSION
    payload["generated_at"] = _utc_now_iso()
    payload["total_images"] = len(pages)
    payload["datasets"] = sorted({str(p.get("dataset", "")) for p in pages if p.get("dataset")})
    _atomic_write_json(summary_path, payload)
    return summary_path


def load_annotation(output_dir: Path, entry: ImageEntry, width: int, height: int) -> PageAnnotation:
    path = annotation_path_for(output_dir, entry)
    if not path.is_file():
        return new_annotation_for_entry(entry, width, height)
    data = json.loads(path.read_text(encoding="utf-8"))
    ann = PageAnnotation.from_dict(data)
    if (ann.image_width, ann.image_height) != (width, height):
        raise ValueError("Kích thước ảnh đã đổi so với annotation; cần kiểm tra lại tọa độ vùng.")
    if (ann.dataset, ann.filename) != (entry.dataset, entry.filename):
        raise ValueError("Annotation không thuộc ảnh đang mở; cần kiểm tra lại nguồn.")
    if ann.pdf_basename is None and entry.pdf_source is not None:
        ann.pdf_basename = entry.pdf_source.pdf_basename
        ann.pdf_page_range = [entry.pdf_source.pdf_page_start, entry.pdf_source.pdf_page_end]
        ann.pdf_page_index = entry.pdf_page_index
    return ann


def session_path_for(output_dir: Path) -> Path:
    return output_dir / "session.json"


def save_session(output_dir: Path, entry: ImageEntry, index: int) -> Path:
    path = session_path_for(output_dir)
    _atomic_write_json(
        path,
        {
            "last_image_key": entry.key,
            "last_index": int(index),
            "updated_at": _utc_now_iso(),
        },
    )
    return path


def load_session(output_dir: Path) -> dict | None:
    path = session_path_for(output_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def export_crops(
    entry: ImageEntry,
    annotation: PageAnnotation,
    export_root: Path,
    *,
    selected_ids: set[str] | None = None,
    include_non_content: bool = False,
) -> Path:
    """Xuất ảnh crop độ phân giải gốc cùng manifest.json liên kết nguồn và thứ tự đọc."""
    regions = normalize_reading_orders(annotation.regions)
    targets: list[Region] = []
    for reg in regions:
        if selected_ids is not None and reg.id not in selected_ids:
            continue
        if not include_non_content and reg.label in NON_CONTENT_LABELS:
            continue
        targets.append(reg)

    target_dir = export_root / entry.dataset / entry.stem
    target_dir.mkdir(parents=True, exist_ok=True)

    orientation = normalize_orientation(annotation.orientation)
    manifest_items: list[dict] = []

    with Image.open(entry.path) as img:
        orig_w, orig_h = img.size
        for reg in targets:
            x1, y1, x2, y2 = clamp_bbox(reg.bbox, orig_w, orig_h)
            ix1, iy1, ix2, iy2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            crop = img.crop((ix1, iy1, ix2, iy2))
            if orientation != 0:
                crop = crop.rotate(-orientation, expand=True)
            crop_name = f"order_{reg.reading_order:04d}_{reg.label}_{reg.id}.png"
            crop_path = target_dir / crop_name
            crop.save(crop_path, format="PNG")
            manifest_items.append(
                {
                    "crop_file": crop_name,
                    "crop_path": str(crop_path),
                    "region_id": reg.id,
                    "reading_order": reg.reading_order,
                    "label": reg.label,
                    "language": reg.language,
                    "content_role": reg.content_role,
                    "direction": reg.direction,
                    "review_state": reg.review_state,
                    "bbox_original": [ix1, iy1, ix2, iy2],
                    "orientation": orientation,
                    "crop_width": crop.width,
                    "crop_height": crop.height,
                }
            )

    manifest_path = target_dir / "manifest.json"
    _atomic_write_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "dataset": entry.dataset,
            "source_filename": entry.filename,
            "source_image": str(entry.path),
            "source_dimensions": [annotation.image_width, annotation.image_height],
            "orientation": orientation,
            "source_context": {
                "page_index": annotation.page_index,
                "image_part": annotation.image_part,
                "pdf_basename": annotation.pdf_basename,
                "pdf_page_range": annotation.pdf_page_range,
                "pdf_page_index": annotation.pdf_page_index,
            },
            "exported_at": _utc_now_iso(),
            "crops": manifest_items,
        },
    )
    return manifest_path
