"""Image Packing: ghép các bounding box của mỗi trang thành 1 ảnh mới.

Các vùng (region) của từng ảnh trong `data/01_QuocTrieuHuongKhoaLuc` và
`data/02_TuLiemDaiKhoaLuc` được đọc từ `results/layout_all.json` hoặc từ các tệp
JSON trong `outputs/regions/<dataset>/`, sau đó cắt và ghép theo chiều ngang,
từ phải qua trái (RTL), có giãn cách nhẹ, khung bounding box và số thứ tự.

Ví dụ chạy từ thư mục gốc dự án:
    python3 -m source.image_packing
    python3 -m source.image_packing --source regions --output-dir outputs/packed_regions
    python3 -m source.image_packing --gap 32 --padding 40 --limit 10
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

PACK_MODES = ("rtl_row", "rtl_stack", "maxrects")

DEFAULT_DATASETS = (
    "01_QuocTrieuHuongKhoaLuc",
    "02_TuLiemDaiKhoaLuc",
)

LABEL_COLORS: dict[str, tuple[int, int, int]] = {
    "title": (220, 38, 38),
    "body": (37, 99, 235),
    "header": (217, 119, 6),
    "footer": (100, 116, 139),
    "ignore": (148, 163, 184),
}

DEFAULT_COLOR = (22, 163, 74)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)


@dataclass(frozen=True)
class RegionItem:
    region_id: str
    bbox: tuple[float, float, float, float]
    label: str
    reading_order: int | None


@dataclass(frozen=True)
class PageSpec:
    dataset: str
    filename: str
    image_path: Path
    regions: list[RegionItem]


@dataclass(frozen=True)
class PackConfig:
    mode: str = "rtl_row"
    gap: int = 28
    padding: int = 36
    border_width: int = 4
    font_size: int | None = None
    align: str = "top"
    max_width: int | None = None
    row_gap: int = 36
    bg_color: tuple[int, int, int] = (255, 255, 255)
    draw_bbox: bool = True
    draw_index: bool = True


@dataclass(frozen=True)
class _CropEntry:
    index: int
    region: RegionItem
    orig_bbox: tuple[int, int, int, int]
    crop: Image.Image
    slot_w: int
    slot_h: int


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _parse_region(raw: dict) -> RegionItem | None:
    bbox = raw.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = (float(v) for v in bbox)
    order = raw.get("reading_order")
    if order is None:
        order = raw.get("position")
    order_int = int(order) if isinstance(order, (int, float)) else None
    return RegionItem(
        region_id=str(raw.get("id", "")),
        bbox=(x1, y1, x2, y2),
        label=str(raw.get("label") or "body"),
        reading_order=order_int,
    )


def _filter_and_sort_regions(
    raw_regions: Iterable[dict],
    exclude_labels: set[str],
) -> list[RegionItem]:
    parsed: list[tuple[int, RegionItem]] = []
    for idx, raw in enumerate(raw_regions):
        item = _parse_region(raw)
        if item is None:
            continue
        if item.label.lower() in exclude_labels:
            continue
        parsed.append((idx, item))

    parsed.sort(
        key=lambda pair: (
            pair[1].reading_order is None,
            pair[1].reading_order if pair[1].reading_order is not None else 0,
            pair[0],
        )
    )
    return [item for _, item in parsed]


def load_pages_from_layout_json(
    layout_json: Path,
    data_dir: Path = Path("data"),
    datasets: Iterable[str] | None = None,
    exclude_labels: set[str] | None = None,
) -> list[PageSpec]:
    """Đọc danh sách trang và vùng từ file tổng kết `results/layout_all.json`."""
    excluded = {lbl.lower() for lbl in (exclude_labels or set())}
    allowed_datasets = set(datasets) if datasets else None

    payload = json.loads(layout_json.read_text(encoding="utf-8"))
    pages: list[PageSpec] = []
    for entry in payload.get("pages", []):
        dataset = str(entry.get("dataset", ""))
        filename = str(entry.get("filename", ""))
        if allowed_datasets is not None and dataset not in allowed_datasets:
            continue

        rel_path = entry.get("relative_path")
        img_path = Path(rel_path) if rel_path else data_dir / dataset / filename
        if not img_path.is_file():
            img_path = data_dir / dataset / filename

        regions = _filter_and_sort_regions(entry.get("bboxes", []), excluded)
        pages.append(
            PageSpec(
                dataset=dataset,
                filename=filename,
                image_path=img_path,
                regions=regions,
            )
        )
    return pages


def load_pages_from_regions_dir(
    regions_dir: Path,
    data_dir: Path = Path("data"),
    datasets: Iterable[str] | None = None,
    exclude_labels: set[str] | None = None,
) -> list[PageSpec]:
    """Đọc danh sách trang và vùng từ thư mục `outputs/regions/<dataset>/*.json`."""
    excluded = {lbl.lower() for lbl in (exclude_labels or set())}
    target_datasets = list(datasets) if datasets else list(DEFAULT_DATASETS)

    pages: list[PageSpec] = []
    for dataset in target_datasets:
        ds_dir = regions_dir / dataset
        if not ds_dir.is_dir():
            continue
        for json_path in sorted(ds_dir.glob("*.json")):
            entry = json.loads(json_path.read_text(encoding="utf-8"))
            filename = str(entry.get("filename") or f"{json_path.stem}.png")
            img_path = data_dir / dataset / filename
            if not img_path.is_file() and entry.get("image_path"):
                img_path = Path(entry["image_path"])

            regions = _filter_and_sort_regions(entry.get("regions", []), excluded)
            pages.append(
                PageSpec(
                    dataset=dataset,
                    filename=filename,
                    image_path=img_path,
                    regions=regions,
                )
            )
    return pages


def _clamp_bbox(
    bbox: tuple[float, float, float, float],
    img_w: int,
    img_h: int,
) -> tuple[int, int, int, int] | None:
    x1 = max(0, min(img_w, int(round(bbox[0]))))
    y1 = max(0, min(img_h, int(round(bbox[1]))))
    x2 = max(0, min(img_w, int(round(bbox[2]))))
    y2 = max(0, min(img_h, int(round(bbox[3]))))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _auto_font_size(crops: list[tuple[RegionItem, tuple[int, int, int, int], Image.Image]]) -> int:
    min_side = min(min(c.width, c.height) for _, _, c in crops)
    median_w = sorted(c.width for _, _, c in crops)[len(crops) // 2]
    size = int(round(min(median_w * 0.16, max(28, min_side * 0.25))))
    return max(26, min(68, size))


def _aspect_penalty(w: int, h: int) -> float:
    ar = max(w / max(1, h), h / max(1, w))
    if ar <= 1.8:
        return 1.0
    return 1.0 + 0.15 * (ar - 1.8)


def _layout_rtl_row(
    entries: list[_CropEntry],
    config: PackConfig,
    label_band_h: int,
) -> tuple[int, int, list[tuple[_CropEntry, int, int]]]:
    """Ghép theo từng hàng ngang từ phải qua trái (mặc định 1 hàng duy nhất)."""
    rows: list[list[_CropEntry]] = []
    current_row: list[_CropEntry] = []
    current_row_w = 0

    for entry in entries:
        projected_w = entry.slot_w if not current_row else current_row_w + config.gap + entry.slot_w
        if (
            config.max_width
            and config.max_width > 0
            and current_row
            and projected_w + 2 * config.padding > config.max_width
        ):
            rows.append(current_row)
            current_row = [entry]
            current_row_w = entry.slot_w
        else:
            current_row.append(entry)
            current_row_w = projected_w

    if current_row:
        rows.append(current_row)

    row_widths = [
        sum(e.slot_w for e in row) + config.gap * max(0, len(row) - 1)
        for row in rows
    ]
    row_heights = [max(e.slot_h for e in row) for row in rows]

    canvas_w = max(row_widths) + 2 * config.padding
    canvas_h = (
        2 * config.padding
        + sum(label_band_h + rh for rh in row_heights)
        + config.row_gap * max(0, len(rows) - 1)
    )

    positioned: list[tuple[_CropEntry, int, int]] = []
    row_top_y = config.padding

    for row, row_h in zip(rows, row_heights):
        cursor_right_x = canvas_w - config.padding
        boxes_top_y = row_top_y + label_band_h

        for entry in row:
            slot_x2 = cursor_right_x
            slot_x1 = slot_x2 - entry.slot_w

            if config.align == "center":
                slot_y1 = boxes_top_y + (row_h - entry.slot_h) // 2
            elif config.align == "bottom":
                slot_y1 = boxes_top_y + (row_h - entry.slot_h)
            else:
                slot_y1 = boxes_top_y

            positioned.append((entry, slot_x1, slot_y1))
            cursor_right_x = slot_x1 - config.gap

        row_top_y = boxes_top_y + row_h + config.row_gap

    return canvas_w, canvas_h, positioned


def _layout_rtl_stack(
    entries: list[_CropEntry],
    config: PackConfig,
    label_band_h: int,
) -> tuple[int, int, list[tuple[_CropEntry, int, int]]]:
    """Xếp chồng cột tối ưu kích thước, giữ nguyên thứ tự đọc 1..N.

    - Trang dọc (Hán Nôm): xếp cột từ phải qua trái, các box ngắn liên tiếp chồng dọc trong cùng cột.
    - Trang ngang (chữ Quốc ngữ): xếp dọc trên xuống dưới và chia cột khi cần để tránh ảnh siêu dài.
    """
    horiz_count = sum(1 for e in entries if e.crop.width > e.crop.height * 1.4)
    is_horizontal_page = horiz_count > len(entries) / 2

    block_heights = [label_band_h + e.slot_h for e in entries]
    min_col_h = max(block_heights)
    total_block_h = sum(block_heights) + config.row_gap * max(0, len(entries) - 1)
    total_area = sum(
        (e.slot_w + config.gap) * (bh + config.row_gap)
        for e, bh in zip(entries, block_heights)
    )
    ideal_side = int(round(math.sqrt(max(1, total_area))))

    candidates: set[int] = {min_col_h, total_block_h}
    for factor in (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6, 2.0):
        h_cand = int(round(ideal_side * factor))
        if h_cand >= min_col_h:
            candidates.add(h_cand)
    # Thử thêm các mốc ghép đôi 2 box liên tiếp ngắn
    for i in range(len(block_heights) - 1):
        pair_h = block_heights[i] + config.row_gap + block_heights[i + 1]
        if pair_h >= min_col_h:
            candidates.add(pair_h)

    best_score: tuple[int, float] | None = None
    best_cols: list[list[_CropEntry]] = [[e] for e in entries]

    for h_limit in sorted(candidates):
        cols: list[list[_CropEntry]] = []
        cur_col: list[_CropEntry] = []
        cur_h = 0

        for entry, bh in zip(entries, block_heights):
            projected_h = bh if not cur_col else cur_h + config.row_gap + bh
            if cur_col and projected_h > h_limit:
                cols.append(cur_col)
                cur_col = [entry]
                cur_h = bh
            else:
                cur_col.append(entry)
                cur_h = projected_h
        if cur_col:
            cols.append(cur_col)

        col_ws = [max(e.slot_w for e in col) for col in cols]
        col_hs = [
            sum(label_band_h + e.slot_h for e in col) + config.row_gap * max(0, len(col) - 1)
            for col in cols
        ]
        used_w = sum(col_ws) + config.gap * max(0, len(cols) - 1)
        used_h = max(col_hs)

        exceeds_max_w = 1 if (
            config.max_width and config.max_width > 0 and used_w + 2 * config.padding > config.max_width
        ) else 0
        score = (exceeds_max_w, used_w * used_h * _aspect_penalty(used_w, used_h))
        if best_score is None or score < best_score:
            best_score = score
            best_cols = cols

    col_ws = [max(e.slot_w for e in col) for col in best_cols]
    col_hs = [
        sum(label_band_h + e.slot_h for e in col) + config.row_gap * max(0, len(col) - 1)
        for col in best_cols
    ]
    canvas_w = sum(col_ws) + config.gap * max(0, len(best_cols) - 1) + 2 * config.padding
    canvas_h = max(col_hs) + 2 * config.padding

    positioned: list[tuple[_CropEntry, int, int]] = []
    if is_horizontal_page:
        # Trang ngang: đặt các cột từ trái qua phải, mỗi cột từ trên xuống dưới.
        cursor_x = config.padding
        for col, cw in zip(best_cols, col_ws):
            cursor_y = config.padding
            for entry in col:
                slot_x1 = cursor_x + (cw - entry.slot_w) // 2
                slot_y1 = cursor_y + label_band_h
                positioned.append((entry, slot_x1, slot_y1))
                cursor_y = slot_y1 + entry.slot_h + config.row_gap
            cursor_x += cw + config.gap
    else:
        # Trang dọc Hán Nôm: đặt các cột từ phải qua trái, mỗi cột từ trên xuống dưới.
        cursor_right_x = canvas_w - config.padding
        for col, cw in zip(best_cols, col_ws):
            col_x1 = cursor_right_x - cw
            cursor_y = config.padding
            for entry in col:
                slot_x1 = col_x1 + (cw - entry.slot_w) // 2
                slot_y1 = cursor_y + label_band_h
                positioned.append((entry, slot_x1, slot_y1))
                cursor_y = slot_y1 + entry.slot_h + config.row_gap
            cursor_right_x = col_x1 - config.gap

    return canvas_w, canvas_h, positioned


def _prune_free_rects(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    pruned: list[tuple[int, int, int, int]] = []
    for i, (ax, ay, aw, ah) in enumerate(rects):
        ax2, ay2 = ax + aw, ay + ah
        contained = False
        for j, (bx, by, bw, bh) in enumerate(rects):
            if i == j:
                continue
            if bx <= ax and by <= ay and bx + bw >= ax2 and by + bh >= ay2:
                contained = True
                break
        if not contained:
            pruned.append((ax, ay, aw, ah))
    return pruned


def _maxrects_pack_width(
    ordered_items: list[tuple[_CropEntry, int, int]],
    bin_w: int,
    strategy: str,
) -> tuple[int, int, list[tuple[_CropEntry, int, int]]] | None:
    """Đóng gói 2D MaxRects vào dải có chiều rộng `bin_w`, ưu tiên bám góc trên-phải."""
    free_rects: list[tuple[int, int, int, int]] = [(0, 0, bin_w, 10**9)]
    placed: list[tuple[_CropEntry, int, int]] = []

    for entry, item_w, item_h in ordered_items:
        best_score: tuple[int, int, int] | None = None
        best_pos: tuple[int, int] | None = None

        for fx, fy, fw, fh in free_rects:
            if item_w > fw or item_h > fh:
                continue
            # Đặt bám cạnh phải của vùng trống để có xu hướng phải -> trái
            px = fx + fw - item_w
            py = fy
            short_side = min(fw - item_w, fh - item_h)
            if strategy == "bssf":
                score = (short_side, py, -px)
            else:
                score = (py, short_side, -px)
            if best_score is None or score < best_score:
                best_score = score
                best_pos = (px, py)

        if best_pos is None:
            return None

        px, py = best_pos
        px2, py2 = px + item_w, py + item_h
        placed.append((entry, px, py))

        new_free: list[tuple[int, int, int, int]] = []
        for fx, fy, fw, fh in free_rects:
            fx2, fy2 = fx + fw, fy + fh
            if px >= fx2 or px2 <= fx or py >= fy2 or py2 <= fy:
                new_free.append((fx, fy, fw, fh))
                continue
            if px > fx:
                new_free.append((fx, fy, px - fx, fh))
            if px2 < fx2:
                new_free.append((px2, fy, fx2 - px2, fh))
            if py > fy:
                new_free.append((fx, fy, fw, py - fy))
            if py2 < fy2:
                new_free.append((fx, py2, fw, fy2 - py2))

        free_rects = _prune_free_rects(new_free)

    min_x = min(px for _, px, _ in placed)
    max_x = max(px + e.slot_w for e, px, _ in placed)
    min_y = min(py for _, _, py in placed)
    # Chiều cao thực tế không tính row_gap thừa ở cạnh dưới cùng
    return min_x, min_y, placed


def _layout_maxrects(
    entries: list[_CropEntry],
    config: PackConfig,
    label_band_h: int,
) -> tuple[int, int, list[tuple[_CropEntry, int, int]]]:
    """2D Bin Packing (MaxRects) tối ưu diện tích và tỷ lệ khung hình gần vuông."""
    items = [
        (e, e.slot_w + config.gap, label_band_h + e.slot_h + config.row_gap)
        for e in entries
    ]
    ordered_by_side = sorted(items, key=lambda t: (-max(t[1], t[2]), -(t[1] * t[2]), t[0].index))
    ordered_by_area = sorted(items, key=lambda t: (-(t[1] * t[2]), -max(t[1], t[2]), t[0].index))

    min_w = max(iw for _, iw, _ in items)
    max_w = sum(iw for _, iw, _ in items)
    total_area = sum(iw * ih for _, iw, ih in items)
    ideal_w = math.sqrt(max(1, total_area))

    candidates: set[int] = {min_w, max_w}
    for factor in (0.55, 0.7, 0.85, 0.95, 1.05, 1.2, 1.35, 1.55, 1.8, 2.1):
        w_cand = int(round(ideal_w * factor))
        if min_w <= w_cand <= max_w:
            candidates.add(w_cand)

    max_allowed_bin_w: int | None = None
    if config.max_width and config.max_width > 0:
        max_allowed_bin_w = max(min_w, config.max_width - 2 * config.padding + config.gap)
        candidates.add(max_allowed_bin_w)

    best_score: tuple[int, float] | None = None
    best_result: tuple[int, int, int, int, list[tuple[_CropEntry, int, int]]] | None = None

    for bin_w in sorted(candidates):
        for ordered in (ordered_by_side, ordered_by_area):
            for strategy in ("top_right", "bssf"):
                packed = _maxrects_pack_width(ordered, bin_w, strategy)
                if packed is None:
                    continue
                min_x, min_y, placed = packed
                max_x = max(px + e.slot_w for e, px, _ in placed)
                max_y = max(py + label_band_h + e.slot_h for e, _, py in placed)
                used_w = max_x - min_x
                used_h = max_y - min_y

                exceeds_max_w = 1 if (
                    max_allowed_bin_w is not None and used_w + 2 * config.padding > config.max_width
                ) else 0
                score = (exceeds_max_w, used_w * used_h * _aspect_penalty(used_w, used_h))
                if best_score is None or score < best_score:
                    best_score = score
                    best_result = (min_x, min_y, used_w, used_h, placed)

    assert best_result is not None
    min_x, min_y, used_w, used_h, placed = best_result

    canvas_w = used_w + 2 * config.padding
    canvas_h = used_h + 2 * config.padding
    shift_x = config.padding - min_x
    shift_y = config.padding - min_y

    # Sắp xếp lại danh sách output theo thứ tự index 1..N cho manifest sạch đẹp
    placed_sorted = sorted(placed, key=lambda t: t[0].index)
    positioned = [
        (entry, px + shift_x, py + shift_y + label_band_h)
        for entry, px, py in placed_sorted
    ]
    return canvas_w, canvas_h, positioned


def pack_image_regions(
    image: Image.Image,
    regions: list[RegionItem],
    config: PackConfig = PackConfig(),
) -> tuple[Image.Image | None, list[dict]]:
    """Ghép các vùng của 1 ảnh theo chế độ `config.mode` (`rtl_row`, `rtl_stack`, `maxrects`).

    Trả về `(packed_image, placements)`. Nếu không có vùng hợp lệ, trả về `(None, [])`.
    """
    if config.mode not in PACK_MODES:
        raise ValueError(f"Chế độ không hợp lệ: {config.mode}. Chọn một trong {PACK_MODES}.")

    rgb_img = image.convert("RGB")
    img_w, img_h = rgb_img.size

    crops: list[tuple[RegionItem, tuple[int, int, int, int], Image.Image]] = []
    for region in regions:
        clamped = _clamp_bbox(region.bbox, img_w, img_h)
        if clamped is None:
            continue
        crop = rgb_img.crop(clamped)
        crops.append((region, clamped, crop))

    if not crops:
        return None, []

    bw = max(0, config.border_width) if config.draw_bbox else 0
    font_size = config.font_size if (config.font_size and config.font_size > 0) else _auto_font_size(crops)
    font = _load_font(font_size)

    # Đo kích thước nhãn lớn nhất để chừa dải số thứ tự phía trên mỗi box (không đè chữ).
    probe_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if config.draw_index:
        max_label = str(len(crops))
        tb = probe_draw.textbbox((0, 0), max_label, font=font)
        badge_pad_y = max(6, font_size // 5)
        badge_h = (tb[3] - tb[1]) + 2 * badge_pad_y
        label_band_h = badge_h + 10
    else:
        badge_h = 0
        label_band_h = 0

    entries = [
        _CropEntry(
            index=idx,
            region=region,
            orig_bbox=clamped,
            crop=crop,
            slot_w=crop.width + 2 * bw,
            slot_h=crop.height + 2 * bw,
        )
        for idx, (region, clamped, crop) in enumerate(crops, start=1)
    ]

    if config.mode == "rtl_stack":
        canvas_w, canvas_h, positioned = _layout_rtl_stack(entries, config, label_band_h)
    elif config.mode == "maxrects":
        canvas_w, canvas_h, positioned = _layout_maxrects(entries, config, label_band_h)
    else:
        canvas_w, canvas_h, positioned = _layout_rtl_row(entries, config, label_band_h)

    canvas = Image.new("RGB", (canvas_w, canvas_h), config.bg_color)
    draw = ImageDraw.Draw(canvas)

    placements: list[dict] = []
    for entry, slot_x1, slot_y1 in positioned:
        slot_x2 = slot_x1 + entry.slot_w
        crop_x1 = slot_x1 + bw
        crop_y1 = slot_y1 + bw
        crop_x2 = crop_x1 + entry.crop.width
        crop_y2 = crop_y1 + entry.crop.height

        canvas.paste(entry.crop, (crop_x1, crop_y1))

        color = LABEL_COLORS.get(entry.region.label.lower(), DEFAULT_COLOR)
        if config.draw_bbox and bw > 0:
            draw.rectangle(
                [slot_x1, slot_y1, slot_x2 - 1, slot_y1 + entry.slot_h - 1],
                outline=color,
                width=bw,
            )

        if config.draw_index:
            text = str(entry.index)
            tb = draw.textbbox((0, 0), text, font=font)
            tw = tb[2] - tb[0]
            th = tb[3] - tb[1]
            badge_pad_x = max(12, font_size // 2)
            badge_w = max(badge_h, tw + 2 * badge_pad_x)

            cx = (slot_x1 + slot_x2) // 2
            bx1 = max(2, min(canvas_w - badge_w - 2, cx - badge_w // 2))
            by2 = slot_y1 - 6
            by1 = by2 - badge_h
            radius = max(4, badge_h // 5)

            draw.rounded_rectangle(
                [bx1, by1, bx1 + badge_w, by2],
                radius=radius,
                fill=color,
            )
            tx = bx1 + (badge_w - tw) / 2 - tb[0]
            ty = by1 + (badge_h - th) / 2 - tb[1]
            draw.text((tx, ty), text, fill=(255, 255, 255), font=font)

        placements.append(
            {
                "index": entry.index,
                "id": entry.region.region_id,
                "label": entry.region.label,
                "reading_order": entry.region.reading_order,
                "orig_bbox": list(entry.orig_bbox),
                "packed_bbox": [crop_x1, crop_y1, crop_x2, crop_y2],
            }
        )

    return canvas, placements


def pack_dataset(
    pages: list[PageSpec],
    output_dir: Path,
    config: PackConfig = PackConfig(),
    limit: int | None = None,
) -> dict:
    """Chạy Image Packing cho danh sách trang và xuất ảnh + `manifest.json`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = pages[:limit] if (limit is not None and limit > 0) else pages

    records: list[dict] = []
    packed_count = 0
    skipped_empty = 0
    missing_images = 0

    for page in selected:
        if not page.image_path.is_file():
            missing_images += 1
            records.append(
                {
                    "dataset": page.dataset,
                    "filename": page.filename,
                    "source_image": str(page.image_path),
                    "status": "missing_image",
                    "num_regions": 0,
                    "regions": [],
                }
            )
            continue

        with Image.open(page.image_path) as img:
            packed_img, placements = pack_image_regions(img, page.regions, config=config)

        if packed_img is None:
            skipped_empty += 1
            records.append(
                {
                    "dataset": page.dataset,
                    "filename": page.filename,
                    "source_image": str(page.image_path),
                    "status": "empty_regions",
                    "num_regions": 0,
                    "regions": [],
                }
            )
            continue

        out_path = output_dir / page.dataset / page.filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        packed_img.save(out_path)
        packed_count += 1

        box_area = sum(
            (r["packed_bbox"][2] - r["packed_bbox"][0])
            * (r["packed_bbox"][3] - r["packed_bbox"][1])
            for r in placements
        )
        fill_ratio = round(box_area / max(1, packed_img.width * packed_img.height), 4)

        records.append(
            {
                "dataset": page.dataset,
                "filename": page.filename,
                "source_image": str(page.image_path),
                "packed_image": str(out_path),
                "packed_width": packed_img.width,
                "packed_height": packed_img.height,
                "fill_ratio": fill_ratio,
                "status": "ok",
                "num_regions": len(placements),
                "regions": placements,
            }
        )

    ok_ratios = [r["fill_ratio"] for r in records if r["status"] == "ok"]
    avg_fill_ratio = round(sum(ok_ratios) / len(ok_ratios), 4) if ok_ratios else 0.0

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": config.mode,
        "direction": "horizontal_rtl",
        "config": {
            "mode": config.mode,
            "gap": config.gap,
            "padding": config.padding,
            "border_width": config.border_width,
            "font_size": config.font_size,
            "align": config.align,
            "max_width": config.max_width,
            "row_gap": config.row_gap,
        },
        "summary": {
            "total_pages": len(selected),
            "packed": packed_count,
            "skipped_empty": skipped_empty,
            "missing_images": missing_images,
            "avg_fill_ratio": avg_fill_ratio,
        },
        "pages": records,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Image Packing: ghép các bounding box của mỗi trang theo chiều ngang từ phải qua trái."
    )
    parser.add_argument(
        "--mode",
        choices=PACK_MODES,
        default="rtl_row",
        help=(
            "Chế độ ghép ảnh: "
            "'rtl_row' (hàng ngang phải->trái), "
            "'rtl_stack' (xếp chồng cột RTL, tự tách trang ngang), "
            "'maxrects' (2D bin packing tối ưu diện tích & tỷ lệ gần vuông). "
            "Mặc định: rtl_row."
        ),
    )
    parser.add_argument(
        "--source",
        choices=("layout", "regions"),
        default="layout",
        help="Nguồn tọa độ region: 'layout' (results/layout_all.json) hoặc 'regions' (outputs/regions). Mặc định: layout.",
    )
    parser.add_argument(
        "--layout-json",
        type=Path,
        default=Path("results/layout_all.json"),
        help="Đường dẫn tới file tổng kết layout JSON (khi --source layout).",
    )
    parser.add_argument(
        "--regions-dir",
        type=Path,
        default=Path("outputs/regions"),
        help="Thư mục chứa các file region JSON theo dataset (khi --source regions).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Thư mục gốc chứa ảnh theo dataset.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/packed"),
        help="Thư mục lưu ảnh đã ghép và manifest.json.",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=None,
        help="Danh sách tên dataset cần xử lý (mặc định: cả 2 thư mục trong data/).",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=28,
        help="Khoảng giãn cách ngang giữa các bounding box (px). Mặc định: 28.",
    )
    parser.add_argument(
        "--padding",
        type=int,
        default=36,
        help="Lề ngoài của ảnh ghép (px). Mặc định: 36.",
    )
    parser.add_argument(
        "--border-width",
        type=int,
        default=4,
        help="Độ dày viền bounding box (px). Mặc định: 4.",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=0,
        help="Cỡ chữ số thứ tự (0 = tự động theo kích thước box).",
    )
    parser.add_argument(
        "--align",
        choices=("top", "center", "bottom"),
        default="top",
        help="Căn lề dọc của các bounding box trong hàng. Mặc định: top.",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=0,
        help="Chiều rộng tối đa để tự động xuống dòng (0 = ghép tất cả trên 1 hàng ngang).",
    )
    parser.add_argument(
        "--exclude-labels",
        type=str,
        default="header,footer,ignore",
        help="Danh sách nhãn cần bỏ qua, ngăn cách bởi dấu phẩy. Mặc định: header,footer,ignore.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Giới hạn số ảnh xử lý (0 = xử lý tất cả).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    exclude_labels = {s.strip().lower() for s in args.exclude_labels.split(",") if s.strip()}

    if args.source == "layout":
        pages = load_pages_from_layout_json(
            layout_json=args.layout_json,
            data_dir=args.data_dir,
            datasets=args.datasets,
            exclude_labels=exclude_labels,
        )
    else:
        pages = load_pages_from_regions_dir(
            regions_dir=args.regions_dir,
            data_dir=args.data_dir,
            datasets=args.datasets,
            exclude_labels=exclude_labels,
        )

    config = PackConfig(
        mode=args.mode,
        gap=args.gap,
        padding=args.padding,
        border_width=args.border_width,
        font_size=args.font_size if args.font_size > 0 else None,
        align=args.align,
        max_width=args.max_width if args.max_width > 0 else None,
    )
    manifest = pack_dataset(
        pages=pages,
        output_dir=args.output_dir,
        config=config,
        limit=args.limit if args.limit > 0 else None,
    )
    summary = manifest["summary"]
    print(
        f"Image Packing hoàn tất (mode={args.mode}, source={args.source}): "
        f"{summary['packed']}/{summary['total_pages']} ảnh đã ghép, "
        f"lấp đầy TB {summary['avg_fill_ratio'] * 100:.1f}%, "
        f"{summary['skipped_empty']} trang rỗng, "
        f"{summary['missing_images']} thiếu ảnh -> {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
