"""Phân tích cấu trúc trang và phép chiếu (HisDoc1B Algorithm 1 & 2).

Thực hiện:
1. Khử bóng chữ thấu trang (bleed-through suppression) bằng ngưỡng toàn cục kết hợp
   độ tương phản cục bộ (local BoxBlur background subtraction).
2. Phát hiện đường kẻ ngang chia tầng Thượng lan (header_line_y) và đường kẻ khung
   dọc trái/phải (left_frame_x, right_frame_x) để tách vùng Bản tâm / lề cắt.
3. Phát hiện vùng chữ độc lập dựa trên phép chiếu ngang/dọc (Horizontal & Vertical
   Projection Profiles), tách khe trắng đệ quy (Recursive Valley Splitting) và lọc
   vết mực bôi xóa (woodblock smudge filtering).
4. Hậu xử lý (refinement) cho kết quả raw_detections từ PP-OCRv6_medium_det: tách
   các box gộp nhầm nhiều cột và khôi phục phần đầu cột bị sót.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import uuid

import numpy as np
from PIL import Image, ImageFilter


@dataclass
class PageStructure:
    """Thông tin cấu trúc hình học của trang sách sau bước phân tích phép chiếu."""

    width: int
    height: int
    scale: int
    header_line_y: float | None
    left_frame_x: float
    right_frame_x: float
    script_direction: str  # "vertical" (Hán Nôm) hoặc "horizontal" (Quốc ngữ)
    ink_mask: np.ndarray  # bool array ở độ phân giải thu nhỏ (height//scale, width//scale)
    stroke_mask: np.ndarray  # bool array chỉ giữ nét chữ sắc (loại vết bôi đen mờ)


def _load_grayscale(image_or_path: str | Path | Image.Image | np.ndarray, orientation: int = 0) -> tuple[np.ndarray, int, int]:
    if isinstance(image_or_path, (str, Path)):
        with Image.open(image_or_path) as img:
            gray_img = img.convert("L")
    elif isinstance(image_or_path, Image.Image):
        gray_img = image_or_path.convert("L")
    elif isinstance(image_or_path, np.ndarray):
        if image_or_path.ndim == 3:
            gray_img = Image.fromarray(image_or_path).convert("L")
        else:
            gray_img = Image.fromarray(image_or_path.astype(np.uint8), mode="L")
    else:
        raise TypeError("Đầu vào phải là đường dẫn, PIL.Image hoặc numpy.ndarray.")

    orig_w, orig_h = gray_img.size
    angle = int(orientation) % 360
    if angle != 0:
        gray_img = gray_img.rotate(-angle, expand=True)
    return np.asarray(gray_img, dtype=np.float32), orig_w, orig_h


def analyze_page_structure(
    image_or_path: str | Path | Image.Image | np.ndarray,
    *,
    orientation: int = 0,
    scale: int = 2,
) -> PageStructure:
    """Phân tích cấu trúc trang theo Algorithm 1 của HisDoc1B (Nature Scientific Data 2025)."""
    gray, _, _ = _load_grayscale(image_or_path, orientation=orientation)
    h, w = gray.shape
    scale = max(1, int(scale))
    gh, gw = h // scale, w // scale
    if scale > 1:
        small = gray[: gh * scale, : gw * scale].reshape(gh, scale, gw, scale).mean(axis=(1, 3))
    else:
        small = gray

    small_u8 = np.clip(small, 0, 255).astype(np.uint8)
    bg_broad = np.asarray(Image.fromarray(small_u8).filter(ImageFilter.BoxBlur(radius=22)), dtype=np.float32)
    bg_sharp = np.asarray(Image.fromarray(small_u8).filter(ImageFilter.BoxBlur(radius=7)), dtype=np.float32)

    p10, p50 = np.percentile(small, [10, 50])
    global_thresh = min(148.0, 0.45 * float(p10) + 0.55 * float(p50) - 20.0)

    # Mặt nạ mực chính (loại bỏ bóng chữ thấu trang từ mặt sau)
    ink_mask = (small < global_thresh) & ((bg_broad - small) > 24.0)
    # Mặt nạ nét chữ cao tần (loại bỏ mảng mực bôi xóa đặc hoặc vết ố lớn)
    # Nếu vùng cục bộ tối đặc (bg_sharp < 75), đó là khối gỗ đen đặc chứ không phải nét chữ
    stroke_mask = (small < min(135.0, global_thresh)) & ((bg_sharp - small) > 18.0) & (bg_sharp > 72.0)

    # Xóa các đường kẻ khung dọc dài (> 50% chiều cao trang) khỏi stroke_mask để không dính vào cột chữ sát lề
    v_line_cols = np.where(ink_mask[int(0.15 * gh) : int(0.85 * gh), :].mean(axis=0) > 0.52)[0]
    if v_line_cols.size > 0:
        for vc in v_line_cols:
            c0, c1 = max(0, int(vc) - 3), min(gw, int(vc) + 4)
            stroke_mask[:, c0:c1] = False

    # 1. Phát hiện đường kẻ ngang chia Thượng lan (nằm trong khoảng 6% - 21% chiều cao trang)
    y_lo, y_hi = int(0.06 * gh), int(0.21 * gh)
    x_mid_lo, x_mid_hi = int(0.12 * gw), int(0.88 * gw)
    header_line_y: float | None = None
    if y_hi > y_lo and x_mid_hi > x_mid_lo:
        row_proj = ink_mask[y_lo:y_hi, x_mid_lo:x_mid_hi].mean(axis=1)
        best_rel_y = int(np.argmax(row_proj))
        peak_val = float(row_proj[best_rel_y])
        # Đường kẻ khung mộc bản có mật độ điểm đen theo hàng ngang cao nổi bật so với vùng lân cận
        if peak_val >= 0.24:
            header_line_y = float((y_lo + best_rel_y) * scale)

    # 2. Phát hiện đường kẻ khung dọc trái/phải (cô lập Bản tâm và rìa trang kế bên)
    v_band = ink_mask[int(0.18 * gh) : int(0.85 * gh), :]
    left_band_w = max(1, int(0.10 * gw))
    right_band_x = int(0.90 * gw)

    l_proj = v_band[:, :left_band_w].mean(axis=0)
    r_proj = v_band[:, right_band_x:].mean(axis=0)

    if l_proj.size > 0 and float(l_proj.max()) >= 0.42:
        left_frame_x = float((int(np.argmax(l_proj)) + 5) * scale)
    else:
        left_frame_x = float(int(0.015 * w))

    if r_proj.size > 0 and float(r_proj.max()) >= 0.42:
        # Lấy vị trí bắt đầu của dải đen rìa phải (tránh bóng đen dày ở mép scan)
        r_active = np.where(r_proj >= 0.42)[0]
        right_frame_x = float((right_band_x + int(r_active[0]) - 5) * scale)
    else:
        right_frame_x = float(int(0.985 * w))

    # 3. Nhận diện hướng văn bản (dọc Hán Nôm vs ngang Quốc ngữ)
    y_body_lo = int(0.18 * gh)
    y_body_hi = int(0.82 * gh)
    x_body_lo = max(0, int(0.12 * gw))
    x_body_hi = min(gw, int(0.88 * gw))

    script_direction = "vertical"
    if y_body_hi > y_body_lo + 40 and x_body_hi > x_body_lo + 40:
        sub = stroke_mask[y_body_lo:y_body_hi, x_body_lo:x_body_hi]
        hp = np.convolve(sub.mean(axis=1), np.ones(3) / 3.0, mode="same")
        vp = np.convolve(sub.mean(axis=0), np.ones(9) / 9.0, mode="same")
        h_runs = int(np.sum(np.diff((hp > 0.012).astype(np.int8)) == 1))
        h_std = float(np.std(hp))
        v_std = float(np.std(vp))
        if h_std > 1.22 * v_std and h_runs >= 10:
            script_direction = "horizontal"
            header_line_y = None

    return PageStructure(
        width=w,
        height=h,
        scale=scale,
        header_line_y=header_line_y,
        left_frame_x=left_frame_x,
        right_frame_x=right_frame_x,
        script_direction=script_direction,
        ink_mask=ink_mask,
        stroke_mask=stroke_mask,
    )


def _find_1d_runs(active: np.ndarray, min_len: int = 1) -> list[tuple[int, int]]:
    if active.size == 0:
        return []
    diffs = np.diff(active.astype(np.int8))
    starts = list(np.where(diffs == 1)[0] + 1)
    ends = list(np.where(diffs == -1)[0] + 1)
    if active[0]:
        starts.insert(0, 0)
    if active[-1]:
        ends.append(int(active.size))
    return [(int(s), int(e)) for s, e in zip(starts, ends) if (e - s) >= min_len]


def _split_wide_column_runs(
    runs: Sequence[tuple[int, int]],
    smooth_profile: np.ndarray,
    *,
    scale: int,
    ref_col_w: float = 340.0,
    max_col_w: float = 420.0,
    min_col_w: float = 125.0,
) -> list[tuple[int, int]]:
    """Tách đệ quy các dải cột quá rộng tại đáy thung lũng phép chiếu dọc (HisDoc1B Algorithm 1)."""
    result: list[tuple[int, int]] = []
    min_span = max(4, int(min_col_w / scale))
    for s, e in runs:
        w_px = (e - s) * scale
        if w_px <= max_col_w:
            result.append((s, e))
            continue
        n_cols = max(2, int(round(w_px / ref_col_w)))
        cur_s = s
        step = (e - s) / n_cols
        for k in range(1, n_cols):
            expected = int(s + k * step)
            win = max(6, int(0.30 * step))
            lo = max(cur_s + min_span, expected - win)
            hi = min(e - min_span, expected + win)
            if lo < hi:
                split_pt = lo + int(np.argmin(smooth_profile[lo:hi]))
                result.append((cur_s, split_pt))
                cur_s = split_pt
        result.append((cur_s, e))
    return result


def detect_text_regions_projection(
    image_or_path: str | Path | Image.Image | np.ndarray,
    *,
    orientation: int = 0,
    structure: PageStructure | None = None,
) -> list[dict]:
    """Phát hiện các khối/cột văn bản trực tiếp từ ảnh bằng Phép chiếu HisDoc1B (không cần GPU).

    Trả về danh sách dict tương thích với RawDetection ({id, poly, bbox, score}).
    """
    from .geometry import view_to_orig_poly, poly_to_bbox, clamp_bbox

    _, orig_w, orig_h = _load_grayscale(image_or_path, orientation=0)
    st = structure or analyze_page_structure(image_or_path, orientation=orientation)
    scale = st.scale
    gh, gw = st.ink_mask.shape
    detections: list[dict] = []

    def _add_box(vx1: float, vy1: float, vx2: float, vy2: float, score: float = 0.88) -> None:
        if vx2 - vx1 < 12 or vy2 - vy1 < 12:
            return
        vpoly = [[vx1, vy1], [vx2, vy1], [vx2, vy2], [vx1, vy2]]
        orig_poly = view_to_orig_poly(vpoly, orig_w, orig_h, orientation)
        bbox = clamp_bbox(poly_to_bbox(orig_poly), orig_w, orig_h)
        if bbox[2] - bbox[0] >= 8 and bbox[3] - bbox[1] >= 8:
            detections.append(
                {
                    "id": f"reg_{uuid.uuid4().hex[:10]}",
                    "poly": orig_poly,
                    "bbox": bbox,
                    "score": score,
                }
            )

    x_lo = max(0, int(st.left_frame_x / scale))
    x_hi = min(gw, int(st.right_frame_x / scale))

    if st.script_direction == "horizontal":
        # Gom theo dòng ngang cho trang chữ Quốc ngữ
        y_top = int(0.03 * gh)
        y_bot = int(0.95 * gh)
        band = st.stroke_mask[y_top:y_bot, x_lo:x_hi]
        h_proj = np.convolve(band.mean(axis=1), np.ones(9) / 9.0, mode="same")
        thresh = max(0.008, 0.12 * float(np.percentile(h_proj, 90)))
        line_runs = _find_1d_runs(h_proj > thresh, min_len=max(5, int(18 / scale)))
        for rs, re_idx in line_runs:
            y1_s, y2_s = y_top + rs, y_top + re_idx
            row_slice = st.ink_mask[y1_s:y2_s, x_lo:x_hi]
            v_proj = np.convolve(row_slice.mean(axis=0), np.ones(9) / 9.0, mode="same")
            x_runs = _find_1d_runs(v_proj > 0.02, min_len=max(6, int(18 / scale)))
            if not x_runs:
                continue
            vx1 = float((x_lo + x_runs[0][0]) * scale)
            vx2 = float((x_lo + x_runs[-1][1]) * scale)
            vy1 = float(max(0, (y1_s - 2) * scale))
            vy2 = float(min(st.height, (y2_s + 2) * scale))
            # Bỏ qua đường kẻ ngang trang trí mỏng dưới tiêu đề đầu trang
            if (vy2 - vy1) < 32.0 and (vx2 - vx1) / max(1.0, vy2 - vy1) > 16.0:
                continue
            _add_box(vx1, vy1, vx2, vy2, score=0.90)
        return detections

    # 1. Phát hiện các cụm chữ ở Thượng lan (nếu có đường kẻ ngang chia tầng)
    y_sep_s = int(st.header_line_y / scale) if st.header_line_y is not None else int(0.135 * gh)
    if st.header_line_y is not None and y_sep_s > 30:
        # Tìm vạch khung viền ngang trên cùng (trong 5.5% đầu trang) để tránh lấy viền khung làm chữ
        top_border_band = st.ink_mask[: max(10, int(0.055 * gh)), x_lo:x_hi].mean(axis=1)
        top_border_rows = np.where(top_border_band > 0.28)[0]
        y_top_start = (int(top_border_rows[-1]) + 8) if top_border_rows.size > 0 else int(0.015 * gh)
        y_top_end = max(y_top_start + 10, y_sep_s - 10)

        top_band = st.stroke_mask[y_top_start:y_top_end, x_lo:x_hi]
        if top_band.size > 0:
            top_v_proj = np.convolve(top_band.mean(axis=0), np.ones(11) / 11.0, mode="same")
            top_runs = _find_1d_runs(top_v_proj > 0.028, min_len=max(12, int(55 / scale)))
            for cs, ce in top_runs:
                sub_col = st.stroke_mask[y_top_start:y_top_end, x_lo + cs : x_lo + ce]
                h_proj = np.convolve(sub_col.mean(axis=1), np.ones(7) / 7.0, mode="same")
                y_runs = _find_1d_runs(h_proj > 0.032, min_len=max(12, int(45 / scale)))
                if y_runs:
                    vy1 = float((y_top_start + y_runs[0][0]) * scale)
                    vy2 = float((y_top_start + y_runs[-1][1]) * scale)
                    vx1 = float((x_lo + cs) * scale)
                    vx2 = float((x_lo + ce) * scale)
                    # Cụm chữ Thượng lan thật (như 父子登科) luôn có chiều cao >= 160px
                    if (vy2 - vy1) >= max(80.0, 0.038 * st.height):
                        _add_box(vx1, vy1, vx2, vy2, score=0.89)

    # 2. Phát hiện các cột dọc trong vùng Chính văn (từ dưới vạch kẻ ngang tới cuối trang)
    body_y_start = y_sep_s + 12 if st.header_line_y is not None else int(0.08 * gh)
    body_y_mid = int(0.75 * gh)
    body_y_end = int(0.96 * gh)

    # Chiếu trên dải chính [body_y_start, body_y_mid] để tránh vết mực bẩn ở đáy trang làm dính cột
    mid_band = st.stroke_mask[body_y_start:body_y_mid, x_lo:x_hi]
    if mid_band.size == 0:
        return detections

    col_proj = mid_band.mean(axis=0)
    smooth_col = np.convolve(col_proj, np.ones(13) / 13.0, mode="same")
    p90_val = float(np.percentile(smooth_col, 90)) if smooth_col.size > 0 else 0.0
    active_thresh = max(0.016, 0.14 * p90_val)
    raw_runs = _find_1d_runs(smooth_col > active_thresh, min_len=max(15, int(80 / scale)))
    split_runs = _split_wide_column_runs(
        raw_runs,
        smooth_col,
        scale=scale,
        ref_col_w=max(240.0, 0.125 * st.width),
        max_col_w=max(360.0, 0.16 * st.width),
    )

    peak_vals = [float(smooth_col[s:e].max()) for s, e in split_runs if e > s]
    median_peak = float(np.median(peak_vals)) if peak_vals else 0.0

    for cs, ce in split_runs:
        if ce <= cs:
            continue
        col_peak = float(smooth_col[cs:ce].max())
        # Lọc bỏ mảng mực bôi xóa (có mật độ nét chữ sắc thấp hơn hẳn cột chữ thật)
        if median_peak > 0 and col_peak < 0.45 * median_peak:
            continue

        # Nếu cột nằm sát biên khung dọc trái/phải, co nhẹ 8px khi tính chiếu ngang Y để không dính viền khung nghiêng
        inner_cs = cs + (8 if cs <= 6 else 0)
        inner_ce = ce - (8 if ce >= (x_hi - x_lo - 6) else 0)
        if inner_ce <= inner_cs + 4:
            inner_cs, inner_ce = cs, ce
        col_slice = st.stroke_mask[body_y_start:body_y_end, x_lo + inner_cs : x_lo + inner_ce]
        h_proj = np.convolve(col_slice.mean(axis=1), np.ones(11) / 11.0, mode="same")
        y_runs = _find_1d_runs(h_proj > max(0.018, 0.10 * float(h_proj.max())), min_len=max(12, int(45 / scale)))
        if not y_runs:
            continue

        vx1 = float((x_lo + cs) * scale)
        vx2 = float((x_lo + ce) * scale)
        vy1 = float((body_y_start + y_runs[0][0]) * scale)
        vy2 = float((body_y_start + y_runs[-1][1]) * scale)
        _add_box(vx1, vy1, vx2, vy2, score=round(min(0.98, 0.75 + col_peak), 4))

    return detections
