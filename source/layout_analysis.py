"""Layout detection cho một ảnh bằng Surya OCR 0.17.1.

Cài dependency trong môi trường Python đang dùng:
    python -m pip install -r requirements.txt

Surya 0.17.x cần Transformers 4.x; Transformers 5 bỏ thuộc tính pad_token_id
mà decoder của Surya vẫn sử dụng: https://github.com/datalab-to/surya/issues/484

Ví dụ từ thư mục gốc project:
    from source.layout_analysis import SuryaLayoutDetector, visualize_layout

    detector = SuryaLayoutDetector()  # Hoặc device="cuda" / device="cpu".
    image_path = "path/to/page.jpg"
    result = detector(image_path)
    layout_dict = result.model_dump(mode="json")
    fig, ax = visualize_layout(image_path, result)
    fig.savefig("layout_result.png", dpi=150, bbox_inches="tight")
"""

import sys
from importlib.metadata import version
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle
from packaging.version import Version
from PIL import Image


class SuryaLayoutDetector:
    """Một ảnh -> LayoutResult gốc của Surya; khởi tạo model một lần để tái sử dụng."""

    def __init__(self, device=None):
        transformers_version = version("transformers")
        if not Version("4.56.1") <= Version(transformers_version) < Version("5"):
            raise RuntimeError(
                f"Surya 0.17.x không tương thích với transformers=={transformers_version}. "
                "Cần transformers>=4.56.1,<5 (khuyến nghị 4.57.3). "
                f"Chạy: {sys.executable} -m pip install 'transformers==4.57.3'"
            )

        from surya.foundation import FoundationPredictor
        from surya.layout import LayoutPredictor
        from surya.settings import settings

        foundation = FoundationPredictor(
            checkpoint=settings.LAYOUT_MODEL_CHECKPOINT,
            device=device or settings.TORCH_DEVICE_MODEL,
        )
        self.predictor = LayoutPredictor(foundation)

    @staticmethod
    def load_image(image):
        """Nhận đường dẫn, PIL.Image hoặc numpy uint8 (RGB/grayscale; không phải BGR)."""
        if isinstance(image, (str, Path)):
            with Image.open(image) as img:
                return img.convert("RGB")
        if isinstance(image, np.ndarray):
            return Image.fromarray(image).convert("RGB")
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        raise TypeError("image phải là đường dẫn, PIL.Image hoặc numpy.ndarray.")

    def predict(self, image, as_dict=False):
        """Giữ nguyên nhãn, reading order và tọa độ pixel trên ảnh đầu vào.

        result.bboxes: LayoutBox có bbox=[x1,y1,x2,y2], polygon,
        label, position, confidence, top_k. result.image_bbox=[0,0,W,H].
        as_dict=True: xuất dict JSON-compatible theo schema Surya.
        """
        result = self.predictor([self.load_image(image)], batch_size=1)[0]
        return result.model_dump(mode="json") if as_dict else result

    __call__ = predict


def visualize_layout(image, result, figsize=(12, 16), show=True):
    """Vẽ bbox, nhãn, reading order và confidence; nhận LayoutResult hoặc dict.

    Trả (fig, ax) để lưu ảnh bằng fig.savefig(...). Không sửa ảnh/kết quả gốc.
    """
    img = SuryaLayoutDetector.load_image(image)
    data = result if isinstance(result, dict) else result.model_dump(mode="json")
    if list(data["image_bbox"]) != [0, 0, img.width, img.height]:
        raise ValueError("Hãy visualize trên đúng ảnh và kích thước đã đưa vào predict().")

    boxes = data["bboxes"]
    labels = sorted({box["label"] for box in boxes})
    palette = plt.get_cmap("tab20")
    colors = {label: palette(i % 20) for i, label in enumerate(labels)}
    fig, ax = plt.subplots(figsize=figsize)
    ax.imshow(img)
    for box in boxes:
        x1, y1, x2, y2 = box["bbox"]
        color = colors[box["label"]]
        ax.add_patch(Rectangle(
            (x1, y1), x2 - x1, y2 - y1,
            linewidth=2, edgecolor=color, facecolor="none",
        ))
        caption = f'{box["position"]}: {box["label"]}'
        if box.get("confidence") is not None:
            caption += f' ({box["confidence"]:.2f})'
        ax.text(
            x1, max(0, y1 - 3), caption, fontsize=9, color="black",
            va="bottom", bbox=dict(facecolor=color, alpha=0.85, edgecolor="none"),
        )
    ax.set_xlim(0, img.width)
    ax.set_ylim(img.height, 0)
    ax.set_title(f"Surya layout: {len(boxes)} regions")
    ax.axis("off")
    fig.tight_layout()
    if show:
        plt.show()
    return fig, ax

if __name__ == "__main__":
    detector = SuryaLayoutDetector(device="cuda")
    result = detector("data/01_QuocTrieuHuongKhoaLuc/page6_2.png")
    visualize_layout("data/01_QuocTrieuHuongKhoaLuc/page6_2.png", result)
