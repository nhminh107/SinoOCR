"""Thuật toán Layout Analysis cho tư liệu Hán Nôm & Quốc ngữ (PP-OCRv6_medium_det + HisDoc1B).

Giải quyết triệt để các hạn chế của Surya OCR trên sách cổ Hán Nôm:
- Khử bóng chữ thấu trang (bleed-through) và lọc mảnh chữ cắt rìa trang đôi.
- Tách tầng Thượng lan (trên đường kẻ ngang) và lề Bản tâm (ngoài khung dọc).
- Gom cột dọc trọn chiều cao, tự động gộp tiểu chú 2 hàng chữ nhỏ song hành (夹注).
- Phân biệt rõ `title` (Thượng lan & cột Tiêu đề trường thi/khoa thi nhô cao Đài đầu)
  với `body` (cột nội dung cử nhân/tiến sĩ) và `header`/`footer`.
- Đánh số thứ tự đọc (`reading_order` / `position`) chuẩn Phải qua Trái, Trên xuống Dưới
  (`rtl_ttb`) đối với chữ Hán và Trái qua Phải, Trên xuống Dưới (`ltr_ttb`) đối với chữ Việt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .detection import DEFAULT_MODEL_DIR, DEFAULT_MODEL_NAME, RegionDetector
from .geometry import (
    PROJECT_DIR,
    RawDetection,
    Region,
    group_detections,
    normalize_orientation,
)
from .projection import PageStructure, analyze_page_structure


LABEL_COLORS: dict[str, tuple[int, int, int]] = {
    "title": (220, 38, 38),      # Đỏ đậm cho Tiêu đề (Thượng lan & Đài đầu)
    "Title": (220, 38, 38),
    "body": (37, 99, 235),       # Xanh dương cho Cột nội dung chính
    "Text": (37, 99, 235),
    "header": (217, 119, 6),     # Cam vàng cho Đầu trang / Bản tâm
    "Page-header": (217, 119, 6),
    "footer": (100, 116, 139),   # Xám xanh cho Chân trang / Số trang
    "Page-footer": (100, 116, 139),
    "ignore": (148, 163, 184),
}


@dataclass
class LayoutBox:
    """Box kết quả tương thích đồng thời với schema Region và Surya LayoutBox."""

    bbox: list[float]
    label: str
    position: int
    reading_order: int
    direction: str = "vertical"
    language: str = "han"
    confidence: float = 0.95
    id: str = ""
    polygon: list[list[float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.polygon and len(self.bbox) == 4:
            x1, y1, x2, y2 = self.bbox
            self.polygon = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bbox": [round(float(v), 2) for v in self.bbox],
            "polygon": [[round(float(p[0]), 2), round(float(p[1]), 2)] for p in self.polygon],
            "label": self.label,
            "position": int(self.position),
            "reading_order": int(self.reading_order),
            "direction": self.direction,
            "language": self.language,
            "confidence": round(float(self.confidence), 4),
        }


@dataclass
class LayoutResult:
    """Kết quả Layout Analysis cho 1 trang ảnh."""

    bboxes: list[LayoutBox]
    image_bbox: list[float]
    regions: list[Region] = field(default_factory=list)
    raw_detections: list[RawDetection] = field(default_factory=list)
    script_direction: str = "vertical"
    header_line_y: float | None = None
    left_frame_x: float | None = None
    right_frame_x: float | None = None

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "image_bbox": [round(float(v), 2) for v in self.image_bbox],
            "script_direction": self.script_direction,
            "header_line_y": round(float(self.header_line_y), 2) if self.header_line_y is not None else None,
            "left_frame_x": round(float(self.left_frame_x), 2) if self.left_frame_x is not None else None,
            "right_frame_x": round(float(self.right_frame_x), 2) if self.right_frame_x is not None else None,
            "bboxes": [b.to_dict() for b in self.bboxes],
            "regions": [r.to_dict() for r in self.regions],
            "raw_detections": [d.to_dict() for d in self.raw_detections],
        }

    to_dict = model_dump


class SinoLayoutDetector:
    """Bộ phân tích bố cục (Layout Analysis) chuyên biệt cho văn bản Hán Nôm & Quốc ngữ.

    Kết hợp:
    1. PP-OCRv6_medium_det (hoặc cached raw_detections / HisDoc1B projection fallback)
       để phát hiện vùng chữ cấp thấp.
    2. Phân tích cấu trúc trang (HisDoc1B Algorithm 1): bắt đường kẻ ngang Thượng lan
       và đường kẻ khung dọc trái/phải.
    3. Gom cột dọc / dòng ngang, phân loại `title` vs `body` và sắp xếp thứ tự đọc
       Phải -> Trái (`rtl_ttb`) cho chữ Hán hoặc Trái -> Phải (`ltr_ttb`) cho chữ Việt
       (HisDoc1B Algorithm 2).
    """

    def __init__(
        self,
        device: str | None = "gpu:0",
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        engine: str = "auto",
        annotations_dir: Path | str | None = PROJECT_DIR / "outputs" / "regions",
    ) -> None:
        dev = device or "gpu:0"
        if dev == "cuda":
            dev = "gpu:0"
        self.device = dev
        self.annotations_dir = Path(annotations_dir) if annotations_dir is not None else None
        self.detector = RegionDetector(
            model_name=model_name,
            model_dir=model_dir,
            engine=engine,
            device=dev,
            cache_dir=(self.annotations_dir / ".cache" / "paddlex") if self.annotations_dir else None,
        )

    @staticmethod
    def load_image(image: str | Path | Image.Image | np.ndarray) -> Image.Image:
        """Nhận đường dẫn, PIL.Image hoặc numpy uint8 và trả về PIL.Image RGB."""
        if isinstance(image, (str, Path)):
            with Image.open(image) as img:
                return img.convert("RGB")
        if isinstance(image, np.ndarray):
            return Image.fromarray(image).convert("RGB")
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        raise TypeError("image phải là đường dẫn, PIL.Image hoặc numpy.ndarray.")

    def _try_load_cached_raw_detections(
        self, image_path: Path, width: int, height: int
    ) -> list[RawDetection] | None:
        if self.annotations_dir is None:
            return None
        candidate = self.annotations_dir / image_path.parent.name / f"{image_path.stem}.json"
        if not candidate.is_file():
            return None
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if (data.get("image_width"), data.get("image_height")) != (width, height):
                return None
            raws = [RawDetection.from_dict(item) for item in data.get("raw_detections", [])]
            return raws if raws else None
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def predict(
        self,
        image: str | Path | Image.Image | np.ndarray,
        *,
        as_dict: bool = False,
        mode: str | None = None,
        reading_order: str = "rtl_ttb",
        orientation: int = 0,
        use_cached_raw: bool = True,
    ) -> LayoutResult | dict[str, Any]:
        """Phân tích bố cục trang ảnh, phân biệt rõ `title` vs `body` và đánh số thứ tự đọc."""
        pil_img = self.load_image(image)
        orig_w, orig_h = pil_img.size
        angle = normalize_orientation(orientation)

        structure: PageStructure = analyze_page_structure(pil_img, orientation=angle)

        raw_detections: list[RawDetection] | None = None
        if isinstance(image, (str, Path)):
            img_path = Path(image)
            if use_cached_raw:
                raw_detections = self._try_load_cached_raw_detections(img_path, orig_w, orig_h)
            if raw_detections is None:
                raw_detections = self.detector.detect_image(img_path, orientation=angle)
        else:
            from .projection import detect_text_regions_projection
            raw_dicts = detect_text_regions_projection(pil_img, orientation=angle, structure=structure)
            raw_detections = [RawDetection.from_dict(d) for d in raw_dicts]

        if mode is None:
            resolved_mode = (
                "horizontal_lines" if structure.script_direction == "horizontal" else "vertical_columns"
            )
        else:
            resolved_mode = mode

        regions = group_detections(
            raw_detections,
            mode=resolved_mode,
            reading_order=reading_order if resolved_mode == "vertical_columns" else "ltr_ttb",
            language="viet" if resolved_mode == "horizontal_lines" else "han",
            orig_w=orig_w,
            orig_h=orig_h,
            orientation=angle,
            header_line_y=structure.header_line_y,
            left_frame_x=structure.left_frame_x,
            right_frame_x=structure.right_frame_x,
            classify_titles=True,
        )

        bboxes: list[LayoutBox] = []
        for reg in regions:
            # Tính độ tự tin trung bình của các raw box nằm trong vùng này
            rx1, ry1, rx2, ry2 = reg.bbox
            member_scores = [
                d.score
                for d in raw_detections
                if min(rx2, d.bbox[2]) > max(rx1, d.bbox[0]) and min(ry2, d.bbox[3]) > max(ry1, d.bbox[1])
            ]
            conf = float(np.mean(member_scores)) if member_scores else 0.90
            bboxes.append(
                LayoutBox(
                    id=reg.id,
                    bbox=list(reg.bbox),
                    label=reg.label,
                    position=reg.reading_order,
                    reading_order=reg.reading_order,
                    direction=reg.direction,
                    language=reg.language,
                    confidence=conf,
                )
            )

        result = LayoutResult(
            bboxes=bboxes,
            image_bbox=[0.0, 0.0, float(orig_w), float(orig_h)],
            regions=regions,
            raw_detections=raw_detections,
            script_direction="horizontal" if resolved_mode == "horizontal_lines" else "vertical",
            header_line_y=structure.header_line_y,
            left_frame_x=structure.left_frame_x,
            right_frame_x=structure.right_frame_x,
        )
        return result.model_dump(mode="json") if as_dict else result

    __call__ = predict


# Alias tương thích ngược cho các script/notebook cũ từng import SuryaLayoutDetector
SuryaLayoutDetector = SinoLayoutDetector


def render_layout_image(
    image: str | Path | Image.Image | np.ndarray,
    result: LayoutResult | dict[str, Any],
    *,
    draw_structure_lines: bool = True,
) -> Image.Image:
    """Vẽ bounding box, nhãn (title/body/header/footer), số thứ tự đọc và đường cấu trúc bằng PIL."""
    base_img = SinoLayoutDetector.load_image(image).convert("RGBA")
    data = result if isinstance(result, dict) else result.model_dump(mode="json")
    if [round(float(x)) for x in data["image_bbox"]] != [0, 0, base_img.width, base_img.height]:
        raise ValueError("Hãy visualize trên đúng ảnh và kích thước đã đưa vào predict().")

    overlay = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    max_dim = max(base_img.width, base_img.height)
    font_size = max(14, int(round(0.0095 * max_dim)))
    line_w = max(2, int(round(0.0014 * max_dim)))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()

    # Vẽ đường phân cách Thượng lan và khung dọc nếu có
    if draw_structure_lines:
        h_line = data.get("header_line_y")
        if isinstance(h_line, (int, float)):
            draw.line([(0, h_line), (base_img.width, h_line)], fill=(220, 38, 38, 175), width=max(2, line_w))
        for fx_key in ("left_frame_x", "right_frame_x"):
            fx = data.get(fx_key)
            if isinstance(fx, (int, float)) and 20 < fx < base_img.width - 20:
                draw.line([(fx, 0), (fx, base_img.height)], fill=(217, 119, 6, 150), width=max(2, line_w - 1))

    for box in data.get("bboxes", []):
        x1, y1, x2, y2 = [float(v) for v in box["bbox"]]
        label = str(box.get("label", "body"))
        pos = box.get("reading_order", box.get("position", 1))
        conf = box.get("confidence")
        color = LABEL_COLORS.get(label, (22, 163, 74))

        draw.rectangle([x1, y1, x2, y2], fill=(*color, 38), outline=(*color, 255), width=line_w)

        caption = f"#{pos}: {label.upper()}"
        if isinstance(conf, (int, float)):
            caption += f" ({conf:.2f})"
        tb = draw.textbbox((x1, y1), caption, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad_x = max(6.0, 0.25 * font_size)
        pad_y = max(4.0, 0.16 * font_size)
        tag_y1 = max(0.0, y1 - th - 2.0 * pad_y - 2.0)
        tag_x1 = max(0.0, min(x1, base_img.width - tw - 2.0 * pad_x))
        draw.rectangle(
            [tag_x1, tag_y1, tag_x1 + tw + 2.0 * pad_x, tag_y1 + th + 2.0 * pad_y],
            fill=(*color, 235),
        )
        draw.text((tag_x1 + pad_x, tag_y1 + pad_y), caption, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(base_img, overlay).convert("RGB")


def visualize_layout(
    image: str | Path | Image.Image | np.ndarray,
    result: LayoutResult | dict[str, Any],
    figsize: tuple[int, int] = (12, 16),
    show: bool = True,
    save_path: str | Path | None = None,
):
    """Vẽ kết quả Layout Analysis; hỗ trợ lưu trực tiếp bằng PIL hoặc trả về (fig, ax) nếu có matplotlib."""
    vis_img = render_layout_image(image, result)
    if save_path is not None:
        out = Path(save_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        vis_img.save(out)

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return vis_img, None

    fig, ax = plt.subplots(figsize=figsize)
    ax.imshow(vis_img)
    data = result if isinstance(result, dict) else result.model_dump(mode="json")
    boxes = data.get("bboxes", [])
    n_titles = sum(1 for b in boxes if str(b.get("label")).lower() == "title")
    ax.set_title(f"SinoLayout (PP-OCRv6 + HisDoc1B): {len(boxes)} regions ({n_titles} titles)")
    ax.axis("off")
    fig.tight_layout()
    if show:
        plt.show()
    return fig, ax
