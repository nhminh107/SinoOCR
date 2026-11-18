"""Layout Analysis cho tư liệu Hán Nôm & chữ Quốc ngữ (PP-OCRv6_medium_det + HisDoc1B).

Module này cung cấp điểm truy cập tương thích ngược dẫn tới gói `source.LayoutAnalysis`.

Ví dụ sử dụng từ thư mục gốc project:
    from source.LayoutAnalysis import SinoLayoutDetector, visualize_layout
    # Hoặc: from source.layout_analysis import SinoLayoutDetector, visualize_layout

    detector = SinoLayoutDetector()
    image_path = "data/01_QuocTrieuHuongKhoaLuc/page2_1.png"
    result = detector(image_path)
    layout_dict = result.model_dump(mode="json")
    visualize_layout(image_path, result, save_path="results/page2_1_layout.png", show=False)
"""

from __future__ import annotations

from source.LayoutAnalysis.layout_analysis import (
    LABEL_COLORS,
    LayoutBox,
    LayoutResult,
    SinoLayoutDetector,
    SuryaLayoutDetector,
    render_layout_image,
    visualize_layout,
)

__all__ = [
    "LABEL_COLORS",
    "LayoutBox",
    "LayoutResult",
    "SinoLayoutDetector",
    "SuryaLayoutDetector",
    "render_layout_image",
    "visualize_layout",
]

if __name__ == "__main__":
    detector = SinoLayoutDetector(device="gpu:0", engine="auto")
    sample_path = "data/01_QuocTrieuHuongKhoaLuc/page6_2.png"
    res = detector(sample_path)
    visualize_layout(sample_path, res, save_path="results/layout_page6_2.png", show=False)
    print(f"Đã lưu kết quả Layout Analysis ({len(res.bboxes)} vùng) vào results/layout_page6_2.png")
