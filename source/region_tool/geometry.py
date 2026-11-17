"""Cấu trúc dữ liệu, biến đổi tọa độ xoay ảnh, gom nhóm cột/dòng và đánh số đọc."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_FOLDERS = [
    PROJECT_DIR / "data/01_QuocTrieuHuongKhoaLuc",
    PROJECT_DIR / "data/02_TuLiemDaiKhoaLuc",
]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

LABELS = ("body", "title", "header", "footer", "ignore")
NON_CONTENT_LABELS = frozenset({"ignore", "header", "footer"})
LANGUAGES = ("han", "viet", "unknown")
CONTENT_ROLES = ("original", "phonetic", "translation", "other")
DIRECTIONS = ("vertical", "horizontal")
REVIEW_STATES = ("pending", "reviewed", "needs_review")
READING_ORDERS = ("rtl_ttb", "ltr_ttb", "ttb_ltr")

_PAGE_RE = re.compile(r"^page(\d+)(?:_(\d+))?$", re.IGNORECASE)
_PDF_RANGE_RE = re.compile(r"_trang_(\d+)-(\d+)", re.IGNORECASE)
_NATURAL_SPLIT_RE = re.compile(r"(\d+)")


def new_region_id() -> str:
    return f"reg_{uuid.uuid4().hex[:10]}"


def natural_sort_key(text: str) -> list[int | str]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_SPLIT_RE.split(text)
    ]


@dataclass(frozen=True)
class PdfSourceInfo:
    pdf_basename: str
    pdf_page_start: int
    pdf_page_end: int

    def page_context_for(self, page_index: int | None) -> int | None:
        if page_index is None or page_index < 1:
            return None
        candidate = self.pdf_page_start + page_index - 1
        if candidate > self.pdf_page_end:
            return None
        return candidate


@dataclass(frozen=True)
class ImageEntry:
    path: Path
    dataset: str
    filename: str
    stem: str
    page_index: int | None
    image_part: int | None
    pdf_source: PdfSourceInfo | None = None

    @property
    def key(self) -> str:
        return f"{self.dataset}/{self.filename}"

    @property
    def pdf_page_index(self) -> int | None:
        if self.pdf_source is None:
            return None
        return self.pdf_source.page_context_for(self.page_index)


@dataclass
class RawDetection:
    id: str
    poly: list[list[float]]
    bbox: list[float]  # [x1, y1, x2, y2] in original image coordinates
    score: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "poly": [[round(float(x), 2), round(float(y), 2)] for x, y in self.poly],
            "bbox": [round(float(v), 2) for v in self.bbox],
            "score": round(float(self.score), 6),
        }

    @classmethod
    def from_dict(cls, data: dict) -> RawDetection:
        return cls(
            id=str(data.get("id") or new_region_id()),
            poly=[[float(p[0]), float(p[1])] for p in data.get("poly", [])],
            bbox=[float(v) for v in data["bbox"]],
            score=float(data.get("score", 0.0)),
        )


@dataclass
class Region:
    id: str
    bbox: list[float]  # [x1, y1, x2, y2] in original image coordinates
    label: str = "body"
    language: str = "han"
    content_role: str = "original"
    direction: str = "vertical"
    reading_order: int = 1
    review_state: str = "pending"
    notes: str = ""

    def clone(self) -> Region:
        return Region(
            id=self.id,
            bbox=list(self.bbox),
            label=self.label,
            language=self.language,
            content_role=self.content_role,
            direction=self.direction,
            reading_order=self.reading_order,
            review_state=self.review_state,
            notes=self.notes,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bbox": [round(float(v), 2) for v in self.bbox],
            "label": self.label,
            "language": self.language,
            "content_role": self.content_role,
            "direction": self.direction,
            "reading_order": int(self.reading_order),
            "review_state": self.review_state,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Region:
        return cls(
            id=str(data.get("id") or new_region_id()),
            bbox=[float(v) for v in data["bbox"]],
            label=str(data.get("label", "body")),
            language=str(data.get("language", "han")),
            content_role=str(data.get("content_role", "original")),
            direction=str(data.get("direction", "vertical")),
            reading_order=max(1, int(data.get("reading_order", 1))),
            review_state=str(data.get("review_state", "pending")),
            notes=str(data.get("notes", "")),
        )


def parse_image_filename(stem: str) -> tuple[int | None, int | None]:
    """Tách số trang PDF và phần ảnh vật lý (_1/_2), không suy diễn ngôn ngữ."""
    match = _PAGE_RE.match(stem)
    if not match:
        return None, None
    page_index = int(match.group(1))
    image_part = int(match.group(2)) if match.group(2) is not None else None
    return page_index, image_part


def discover_pdf_sources(data_dir: Path) -> dict[str, PdfSourceInfo]:
    """Tìm PDF cùng cấp trong data_dir theo tiền tố 2 chữ số hoặc tên thư mục."""
    mapping: dict[str, PdfSourceInfo] = {}
    if not data_dir.is_dir():
        return mapping
    for pdf_path in sorted(data_dir.glob("*.pdf")):
        range_match = _PDF_RANGE_RE.search(pdf_path.stem)
        if not range_match:
            continue
        info = PdfSourceInfo(
            pdf_basename=pdf_path.name,
            pdf_page_start=int(range_match.group(1)),
            pdf_page_end=int(range_match.group(2)),
        )
        prefix_match = re.match(r"^(\d+)_", pdf_path.name)
        if prefix_match:
            mapping[prefix_match.group(1)] = info
        mapping[pdf_path.stem.lower()] = info
    return mapping


def match_pdf_source(folder: Path, pdf_sources: dict[str, PdfSourceInfo]) -> PdfSourceInfo | None:
    prefix_match = re.match(r"^(\d+)_", folder.name)
    if prefix_match and prefix_match.group(1) in pdf_sources:
        return pdf_sources[prefix_match.group(1)]
    return pdf_sources.get(folder.name.lower())


def collect_images(folders: Sequence[Path]) -> list[ImageEntry]:
    """Quét ảnh theo thứ tự tự nhiên giống quy ước crop_tool.py."""
    entries: list[ImageEntry] = []
    pdf_cache: dict[Path, dict[str, PdfSourceInfo]] = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        parent = folder.parent
        if parent not in pdf_cache:
            pdf_cache[parent] = discover_pdf_sources(parent)
        pdf_info = match_pdf_source(folder, pdf_cache[parent])
        paths = [
            p
            for p in folder.iterdir()
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES
        ]
        paths.sort(key=lambda p: natural_sort_key(p.name))
        for path in paths:
            page_index, image_part = parse_image_filename(path.stem)
            entries.append(
                ImageEntry(
                    path=path,
                    dataset=folder.name,
                    filename=path.name,
                    stem=path.stem,
                    page_index=page_index,
                    image_part=image_part,
                    pdf_source=pdf_info,
                )
            )
    return entries


# ---------------------------------------------------------------------------
# Biến đổi tọa độ giữa ảnh gốc (orig) và góc nhìn xoay bội số 90° theo chiều kim đồng hồ
# ---------------------------------------------------------------------------


def normalize_orientation(degrees: int) -> int:
    value = int(degrees) % 360
    if value not in (0, 90, 180, 270):
        raise ValueError(f"Góc xoay phải là bội số của 90°, nhận được: {degrees}")
    return value


def rotated_dimensions(width: int, height: int, orientation: int) -> tuple[int, int]:
    angle = normalize_orientation(orientation)
    if angle in (90, 270):
        return height, width
    return width, height


def orig_to_view_point(
    x: float, y: float, orig_w: int, orig_h: int, orientation: int
) -> tuple[float, float]:
    angle = normalize_orientation(orientation)
    if angle == 0:
        return x, y
    if angle == 90:
        return orig_h - y, x
    if angle == 180:
        return orig_w - x, orig_h - y
    return y, orig_w - x  # 270


def view_to_orig_point(
    vx: float, vy: float, orig_w: int, orig_h: int, orientation: int
) -> tuple[float, float]:
    angle = normalize_orientation(orientation)
    if angle == 0:
        return vx, vy
    if angle == 90:
        return vy, orig_h - vx
    if angle == 180:
        return orig_w - vx, orig_h - vy
    return orig_w - vy, vx  # 270


def orig_to_view_bbox(
    bbox: Sequence[float], orig_w: int, orig_h: int, orientation: int
) -> list[float]:
    x1, y1, x2, y2 = bbox
    p1 = orig_to_view_point(x1, y1, orig_w, orig_h, orientation)
    p2 = orig_to_view_point(x2, y2, orig_w, orig_h, orientation)
    return [min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])]


def view_to_orig_bbox(
    bbox: Sequence[float], orig_w: int, orig_h: int, orientation: int
) -> list[float]:
    vx1, vy1, vx2, vy2 = bbox
    p1 = view_to_orig_point(vx1, vy1, orig_w, orig_h, orientation)
    p2 = view_to_orig_point(vx2, vy2, orig_w, orig_h, orientation)
    return [min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])]


def view_to_orig_poly(
    poly: Sequence[Sequence[float]], orig_w: int, orig_h: int, orientation: int
) -> list[list[float]]:
    return [list(view_to_orig_point(float(p[0]), float(p[1]), orig_w, orig_h, orientation)) for p in poly]


def clamp_bbox(bbox: Sequence[float], width: int, height: int) -> list[float]:
    x1, y1, x2, y2 = bbox
    nx1 = max(0.0, min(float(width), min(x1, x2)))
    ny1 = max(0.0, min(float(height), min(y1, y2)))
    nx2 = max(0.0, min(float(width), max(x1, x2)))
    ny2 = max(0.0, min(float(height), max(y1, y2)))
    return [nx1, ny1, nx2, ny2]


def poly_to_bbox(poly: Sequence[Sequence[float]]) -> list[float]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------------------
# Gom nhóm cột (chữ Hán) và dòng (chữ Quốc ngữ) từ raw detections
# ---------------------------------------------------------------------------


def _bbox_width(b: Sequence[float]) -> float:
    return max(1.0, b[2] - b[0])


def _bbox_height(b: Sequence[float]) -> float:
    return max(1.0, b[3] - b[1])


def _overlap_1d(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def _merge_bboxes(boxes: Iterable[Sequence[float]]) -> list[float]:
    xs1, ys1, xs2, ys2 = [], [], [], []
    for x1, y1, x2, y2 in boxes:
        xs1.append(x1)
        ys1.append(y1)
        xs2.append(x2)
        ys2.append(y2)
    return [min(xs1), min(ys1), max(xs2), max(ys2)]


def _to_view_detections(
    detections: Sequence[RawDetection],
    orig_w: int | None,
    orig_h: int | None,
    orientation: int,
) -> list[tuple[RawDetection, list[float]]]:
    angle = normalize_orientation(orientation)
    if angle == 0 or orig_w is None or orig_h is None:
        return [(d, list(d.bbox)) for d in detections]
    return [(d, orig_to_view_bbox(d.bbox, orig_w, orig_h, angle)) for d in detections]


def _to_orig_bbox(
    vbox: Sequence[float],
    orig_w: int | None,
    orig_h: int | None,
    orientation: int,
) -> list[float]:
    angle = normalize_orientation(orientation)
    if angle == 0 or orig_w is None or orig_h is None:
        return [float(v) for v in vbox]
    return clamp_bbox(view_to_orig_bbox(vbox, orig_w, orig_h, angle), orig_w, orig_h)


def group_detections_vertical_columns(
    detections: Sequence[RawDetection],
    *,
    min_score: float = 0.25,
    overlap_threshold: float = 0.35,
    center_dx_ratio: float = 0.45,
    max_column_width_ratio: float = 2.2,
    reading_order: str = "rtl_ttb",
    language: str = "han",
    max_vertical_gap_ratio: float | None = None,
    orig_w: int | None = None,
    orig_h: int | None = None,
    orientation: int = 0,
) -> list[Region]:
    """Gom các box OCR thành cột dọc theo góc nhìn hiện tại rồi quy đổi về tọa độ gốc.

    Tránh gộp nhầm hai cột cạnh nhau hoặc để box tiêu đề/đầu trang quá rộng bắc cầu
    nối các cột nội dung tách biệt: giới hạn bề rộng cột hợp nhất theo ref_width.
    """
    filtered = [d for d in detections if d.score >= min_score and _bbox_width(d.bbox) > 0 and _bbox_height(d.bbox) > 0]
    if not filtered:
        return []

    view_items = _to_view_detections(filtered, orig_w, orig_h, orientation)
    widths = [_bbox_width(vb) for _, vb in view_items]
    heights = [_bbox_height(vb) for _, vb in view_items]
    ref_width = float(median(widths))
    ref_height = float(median(heights))
    max_col_w = max_column_width_ratio * ref_width

    # Sắp xếp từ trên xuống dưới trong góc nhìn hiện tại
    ordered = sorted(view_items, key=lambda item: (item[1][1], item[1][0]))
    columns: list[list[list[float]]] = []
    col_x_spans: list[tuple[float, float]] = []

    for _, vbox in ordered:
        x1, _, x2, _ = vbox
        det_w = _bbox_width(vbox)
        det_cx = 0.5 * (x1 + x2)
        best_idx = -1
        best_score = -1.0

        # Box quá rộng (tiêu đề/đầu trang) không được gộp vào cột hẹp để tránh bắc cầu
        if det_w <= max_col_w:
            for idx, (cx1, cx2) in enumerate(col_x_spans):
                col_w = max(1.0, cx2 - cx1)
                if col_w > max_col_w:
                    continue
                merged_w = max(cx2, x2) - min(cx1, x1)
                if merged_w > max_col_w:
                    continue
                col_cx = 0.5 * (cx1 + cx2)
                ov = _overlap_1d(x1, x2, cx1, cx2)
                ov_ratio = ov / min(det_w, col_w)
                dx_ratio = abs(det_cx - col_cx) / max(col_w, ref_width)

                same_col = (ov_ratio >= overlap_threshold) or (
                    cx1 <= det_cx <= cx2 and dx_ratio <= center_dx_ratio
                )
                if not same_col:
                    continue
                score = ov_ratio - 0.5 * dx_ratio
                if score > best_score:
                    best_score = score
                    best_idx = idx

        if best_idx == -1:
            columns.append([vbox])
            col_x_spans.append((x1, x2))
        else:
            columns[best_idx].append(vbox)
            px1, px2 = col_x_spans[best_idx]
            col_x_spans[best_idx] = (min(px1, x1), max(px2, x2))

    # Tách đoạn trong cột nếu khoảng trống dọc vượt ngưỡng (nếu bật)
    blocks: list[list[list[float]]] = []
    for col in columns:
        col_sorted = sorted(col, key=lambda b: (b[1], b[0]))
        if max_vertical_gap_ratio is None:
            blocks.append(col_sorted)
            continue
        max_gap = max_vertical_gap_ratio * ref_height
        current = [col_sorted[0]]
        for item in col_sorted[1:]:
            prev_y2 = max(b[3] for b in current)
            if item[1] - prev_y2 > max_gap:
                blocks.append(current)
                current = [item]
            else:
                current.append(item)
        blocks.append(current)

    block_vboxes = [_merge_bboxes(blk) for blk in blocks]
    if reading_order == "ltr_ttb":
        block_vboxes.sort(key=lambda b: (0.5 * (b[0] + b[2]), b[1]))
    else:
        # Mặc định chữ Hán: cột phải sang trái, cùng cột từ trên xuống dưới
        block_vboxes.sort(key=lambda b: (-0.5 * (b[0] + b[2]), b[1]))

    return [
        Region(
            id=new_region_id(),
            bbox=_to_orig_bbox(vbox, orig_w, orig_h, orientation),
            label="body",
            language=language,
            content_role="original",
            direction="vertical",
            reading_order=idx + 1,
            review_state="pending",
            notes="",
        )
        for idx, vbox in enumerate(block_vboxes)
    ]


def group_detections_horizontal_lines(
    detections: Sequence[RawDetection],
    *,
    min_score: float = 0.25,
    overlap_threshold: float = 0.4,
    max_x_gap_ratio: float = 2.5,
    max_line_height_ratio: float = 2.2,
    language: str = "viet",
    orig_w: int | None = None,
    orig_h: int | None = None,
    orientation: int = 0,
) -> list[Region]:
    """Gom các box OCR thành dòng ngang theo góc nhìn hiện tại rồi quy đổi về tọa độ gốc.

    Giới hạn khoảng trống X tối đa theo chiều cao chữ cục bộ để không gộp nhầm chữ
    cùng hàng Y qua khe giữa 2 trang sách đôi hoặc 2 cột tách biệt.
    """
    filtered = [d for d in detections if d.score >= min_score and _bbox_width(d.bbox) > 0 and _bbox_height(d.bbox) > 0]
    if not filtered:
        return []

    view_items = _to_view_detections(filtered, orig_w, orig_h, orientation)
    ref_height = float(median([_bbox_height(vb) for _, vb in view_items]))
    max_line_h = max_line_height_ratio * ref_height

    ordered = sorted(view_items, key=lambda item: (item[1][0], item[1][1]))
    lines: list[list[list[float]]] = []
    line_spans: list[tuple[float, float, float]] = []  # (y1, y2, max_x2)

    for _, vbox in ordered:
        x1, y1, x2, y2 = vbox
        det_h = _bbox_height(vbox)
        best_idx = -1
        best_ov = -1.0
        for idx, (ly1, ly2, lx2) in enumerate(line_spans):
            line_h = max(1.0, ly2 - ly1)
            if max(ly2, y2) - min(ly1, y1) > max_line_h:
                continue
            local_h = min(det_h, line_h)
            x_gap = x1 - lx2
            if x_gap > max_x_gap_ratio * max(local_h, ref_height):
                continue
            ov = _overlap_1d(y1, y2, ly1, ly2)
            ov_ratio = ov / local_h
            if ov_ratio >= overlap_threshold and ov_ratio > best_ov:
                best_ov = ov_ratio
                best_idx = idx
        if best_idx == -1:
            lines.append([vbox])
            line_spans.append((y1, y2, x2))
        else:
            lines[best_idx].append(vbox)
            py1, py2, px2 = line_spans[best_idx]
            line_spans[best_idx] = (min(py1, y1), max(py2, y2), max(px2, x2))

    line_vboxes = [_merge_bboxes(line) for line in lines]
    line_vboxes.sort(key=lambda b: (0.5 * (b[1] + b[3]), b[0]))

    return [
        Region(
            id=new_region_id(),
            bbox=_to_orig_bbox(vbox, orig_w, orig_h, orientation),
            label="body",
            language=language,
            content_role="original",
            direction="horizontal",
            reading_order=idx + 1,
            review_state="pending",
            notes="",
        )
        for idx, vbox in enumerate(line_vboxes)
    ]


def group_detections(
    detections: Sequence[RawDetection],
    *,
    mode: str = "vertical_columns",
    min_score: float = 0.25,
    overlap_threshold: float = 0.35,
    center_dx_ratio: float = 0.45,
    reading_order: str = "rtl_ttb",
    language: str | None = None,
    max_vertical_gap_ratio: float | None = None,
    orig_w: int | None = None,
    orig_h: int | None = None,
    orientation: int = 0,
) -> list[Region]:
    if mode == "horizontal_lines":
        return group_detections_horizontal_lines(
            detections,
            min_score=min_score,
            overlap_threshold=max(0.2, overlap_threshold),
            language=language or "viet",
            orig_w=orig_w,
            orig_h=orig_h,
            orientation=orientation,
        )
    return group_detections_vertical_columns(
        detections,
        min_score=min_score,
        overlap_threshold=overlap_threshold,
        center_dx_ratio=center_dx_ratio,
        reading_order=reading_order,
        language=language or "han",
        max_vertical_gap_ratio=max_vertical_gap_ratio,
        orig_w=orig_w,
        orig_h=orig_h,
        orientation=orientation,
    )


# ---------------------------------------------------------------------------
# Đánh số đọc hàng loạt, gộp & chia vùng
# ---------------------------------------------------------------------------


def _reading_sort_key(
    region: Region,
    order_mode: str,
    *,
    orig_w: int | None = None,
    orig_h: int | None = None,
    orientation: int = 0,
) -> tuple[float, float, str]:
    angle = normalize_orientation(orientation)
    if angle != 0 and orig_w is not None and orig_h is not None:
        x1, y1, x2, y2 = orig_to_view_bbox(region.bbox, orig_w, orig_h, angle)
    else:
        x1, y1, x2, y2 = region.bbox
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    if order_mode == "rtl_ttb":
        return (-cx, y1, region.id)
    if order_mode == "ltr_ttb":
        return (cx, y1, region.id)
    if order_mode == "ttb_ltr":
        return (cy, x1, region.id)
    raise ValueError(f"Chế độ thứ tự đọc không hợp lệ: {order_mode}")


def bulk_number_regions(
    regions: Sequence[Region],
    selected_ids: set[str],
    *,
    start_index: int = 1,
    order_mode: str = "rtl_ttb",
    exclude_non_content: bool = True,
    orig_w: int | None = None,
    orig_h: int | None = None,
    orientation: int = 0,
) -> list[Region]:
    """Đánh số lại các vùng được chọn một cách xác định và duy nhất toàn cục.

    - Sắp xếp theo tọa độ góc nhìn hiện tại (orientation) nhưng giữ nguyên bbox ảnh gốc.
    - Chỉ các vùng trong selected_ids (và không thuộc ignore/header/footer nếu
      exclude_non_content=True) được đánh số từ start_index theo order_mode.
    - Các vùng còn lại giữ nguyên thứ tự tương đối và được cấp số nguyên dương
      duy nhất tránh trùng với dải số vừa gán.
    """
    if start_index < 1:
        raise ValueError("Chỉ số bắt đầu phải >= 1.")

    cloned = [r.clone() for r in regions]
    target_regions = [
        r
        for r in cloned
        if r.id in selected_ids and (not exclude_non_content or r.label not in NON_CONTENT_LABELS)
    ]
    if not target_regions:
        return normalize_reading_orders(cloned)

    target_ids = {r.id for r in target_regions}
    target_regions.sort(
        key=lambda r: _reading_sort_key(
            r, order_mode, orig_w=orig_w, orig_h=orig_h, orientation=orientation
        )
    )

    taken: set[int] = set()
    for offset, reg in enumerate(target_regions):
        num = start_index + offset
        reg.reading_order = num
        taken.add(num)

    others = [r for r in cloned if r.id not in target_ids]
    others.sort(key=lambda r: (r.reading_order, r.bbox[1], r.bbox[0], r.id))
    candidate = 1
    for reg in others:
        while candidate in taken:
            candidate += 1
        reg.reading_order = candidate
        taken.add(candidate)
        candidate += 1

    return sorted(cloned, key=lambda r: (r.reading_order, r.id))


def normalize_reading_orders(regions: Sequence[Region]) -> list[Region]:
    """Đảm bảo mọi reading_order đều nguyên dương và duy nhất theo thứ tự hiện có."""
    cloned = [r.clone() for r in regions]
    cloned.sort(key=lambda r: (max(1, int(r.reading_order)), r.bbox[1], r.bbox[0], r.id))
    used: set[int] = set()
    for reg in cloned:
        num = max(1, int(reg.reading_order))
        while num in used:
            num += 1
        reg.reading_order = num
        used.add(num)
    return sorted(cloned, key=lambda r: (r.reading_order, r.id))


def merge_regions(regions: Sequence[Region], selected_ids: set[str]) -> tuple[list[Region], str | None]:
    """Gộp các vùng được chọn thành một bounding box bao ngoài."""
    selected = [r for r in regions if r.id in selected_ids]
    if len(selected) < 2:
        return [r.clone() for r in regions], None

    selected.sort(key=lambda r: (r.reading_order, r.id))
    primary = selected[0]
    merged_box = _merge_bboxes(r.bbox for r in selected)
    merged = Region(
        id=primary.id,
        bbox=merged_box,
        label=primary.label,
        language=primary.language,
        content_role=primary.content_role,
        direction=primary.direction,
        reading_order=primary.reading_order,
        review_state=primary.review_state,
        notes=primary.notes,
    )
    remaining = [r.clone() for r in regions if r.id not in selected_ids]
    remaining.append(merged)
    return normalize_reading_orders(remaining), merged.id


def split_region(
    regions: Sequence[Region],
    region_id: str,
    *,
    axis: str,
    split_coord: float | None = None,
) -> tuple[list[Region], tuple[str, str] | None]:
    """Chia một vùng thành 2 vùng con theo trục 'horizontal' (cắt ngang theo Y) hoặc 'vertical' (cắt dọc theo X)."""
    target = next((r for r in regions if r.id == region_id), None)
    if target is None:
        return [r.clone() for r in regions], None

    x1, y1, x2, y2 = target.bbox
    if axis == "horizontal":
        coord = 0.5 * (y1 + y2) if split_coord is None else split_coord
        if not (y1 + 2.0 < coord < y2 - 2.0):
            return [r.clone() for r in regions], None
        box_a = [x1, y1, x2, coord]
        box_b = [x1, coord, x2, y2]
    elif axis == "vertical":
        coord = 0.5 * (x1 + x2) if split_coord is None else split_coord
        if not (x1 + 2.0 < coord < x2 - 2.0):
            return [r.clone() for r in regions], None
        box_a = [x1, y1, coord, y2]
        box_b = [coord, y1, x2, y2]
    else:
        raise ValueError(f"Trục chia không hợp lệ: {axis}")

    part_a = target.clone()
    part_a.id = new_region_id()
    part_a.bbox = box_a
    part_b = target.clone()
    part_b.id = new_region_id()
    part_b.bbox = box_b
    part_b.reading_order = target.reading_order + 1

    updated: list[Region] = []
    for reg in regions:
        if reg.id == region_id:
            updated.extend([part_a, part_b])
        else:
            item = reg.clone()
            if item.reading_order >= part_b.reading_order:
                item.reading_order += 1
            updated.append(item)
    return normalize_reading_orders(updated), (part_a.id, part_b.id)
