"""Wrapper suy luận cục bộ PP-OCRv6_medium_det (lazy loading, hỗ trợ Paddle & ONNX)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .geometry import (
    RawDetection,
    clamp_bbox,
    new_region_id,
    normalize_orientation,
    poly_to_bbox,
    view_to_orig_poly,
)

DEFAULT_MODEL_NAME = "PP-OCRv6_medium_det"
DEFAULT_MODEL_DIR = Path.home() / ".paddlex" / "official_models" / DEFAULT_MODEL_NAME
SUPPORTED_ENGINES = ("paddle", "onnxruntime")


class DetectionBackendError(RuntimeError):
    """Lỗi rõ ràng khi thiếu dependency hoặc thiếu tệp model hợp lệ cho engine đã chọn."""


def _has_onnx_files(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        return False
    return any(model_dir.glob("*.onnx"))


def _has_paddle_files(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        return False
    has_pdiparams = (model_dir / "inference.pdiparams").is_file()
    has_model = (model_dir / "inference.json").is_file() or (model_dir / "inference.pdmodel").is_file()
    return has_pdiparams and has_model


class RegionDetector:
    """Tải lười PP-OCRv6_medium_det để nhận diện vùng chữ trên ảnh hiện tại."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        engine: str = "paddle",
        device: str = "gpu:0",
        cache_dir: Path | None = None,
    ) -> None:
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(f"Engine không hợp lệ: {engine}. Hỗ trợ: {SUPPORTED_ENGINES}")
        self.model_name = model_name
        self.model_dir = Path(model_dir)
        self.engine = engine
        self.device = device
        self.cache_dir = cache_dir
        self._model: Any = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def configure(
        self,
        *,
        model_dir: Path | str | None = None,
        engine: str | None = None,
        device: str | None = None,
    ) -> None:
        changed = False
        if model_dir is not None and Path(model_dir) != self.model_dir:
            self.model_dir = Path(model_dir)
            changed = True
        if engine is not None and engine != self.engine:
            if engine not in SUPPORTED_ENGINES:
                raise ValueError(f"Engine không hợp lệ: {engine}")
            self.engine = engine
            changed = True
        if device is not None and device != self.device:
            self.device = device
            changed = True
        if changed:
            self._model = None

    def ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model

        if not self.model_dir.exists():
            raise DetectionBackendError(
                f"Không tìm thấy thư mục model '{self.model_dir}'. "
                "Vui lòng kiểm tra lại đường dẫn cache model cục bộ."
            )

        if self.engine == "onnxruntime":
            if not _has_onnx_files(self.model_dir):
                raise DetectionBackendError(
                    f"Thư mục '{self.model_dir}' không chứa tệp .onnx hợp lệ cho ONNXRuntime "
                    "(cache PP-OCRv6_medium_det mặc định chỉ có tệp Paddle-native: "
                    "inference.json / inference.pdiparams). "
                    "Hãy cung cấp thư mục model đã chuyển sang ONNX hoặc chuyển sang engine 'paddle'."
                )
        elif self.engine == "paddle":
            if importlib.util.find_spec("paddle") is None:
                raise DetectionBackendError(
                    "Engine 'paddle' yêu cầu gói 'paddlepaddle', nhưng hiện chưa được cài đặt "
                    "trong môi trường Python. Không tự động tải gói hoặc thay thế bằng model OCR khác. "
                    "Hãy cài paddlepaddle phù hợp hoặc dùng engine 'onnxruntime' với thư mục model ONNX."
                )
            if not _has_paddle_files(self.model_dir):
                raise DetectionBackendError(
                    f"Thư mục '{self.model_dir}' thiếu tệp Paddle-native "
                    "(inference.pdiparams và inference.json/inference.pdmodel)."
                )

        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        if self.cache_dir is not None:
            os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(self.cache_dir.resolve()))
        try:
            from paddleocr import TextDetection
        except ImportError as exc:
            raise DetectionBackendError(
                f"Không import được paddleocr.TextDetection: {exc}"
            ) from exc

        kwargs: dict[str, Any] = {
            "model_name": self.model_name,
            "model_dir": str(self.model_dir),
            "device": self.device,
        }
        if self.engine == "onnxruntime":
            kwargs["engine"] = "onnxruntime"

        try:
            self._model = TextDetection(**kwargs)
        except Exception as exc:
            raise DetectionBackendError(
                f"Không khởi tạo được TextDetection ({self.model_name}, engine={self.engine}): {exc}"
            ) from exc
        return self._model

    def detect_image(self, image_path: Path, orientation: int = 0) -> list[RawDetection]:
        """Chạy nhận diện trên ảnh hiện tại (xoay theo góc nhìn nếu cần) và quy đổi về tọa độ ảnh gốc."""
        angle = normalize_orientation(orientation)
        model = self.ensure_loaded()

        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
            orig_w, orig_h = rgb.size
            if angle != 0:
                rgb = rgb.rotate(-angle, expand=True)
            # PaddleX OpenCV pipeline expects BGR channel order.
            arr = np.asarray(rgb)[:, :, ::-1].copy()

        try:
            outputs = model.predict(arr)
        except Exception as exc:
            raise DetectionBackendError(f"Lỗi khi chạy predict trên '{image_path.name}': {exc}") from exc

        return parse_prediction_outputs(outputs, orig_w=orig_w, orig_h=orig_h, orientation=angle)


def _extract_polys_and_scores(item: Any) -> tuple[Any, Any]:
    if isinstance(item, dict):
        if "res" in item and isinstance(item["res"], dict):
            item = item["res"]
        return item.get("dt_polys", []), item.get("dt_scores", [])
    if hasattr(item, "dt_polys") and hasattr(item, "dt_scores"):
        return item.dt_polys, item.dt_scores
    if hasattr(item, "json") and isinstance(item.json, dict):
        res = item.json.get("res", item.json)
        return res.get("dt_polys", []), res.get("dt_scores", [])
    return [], []


def parse_prediction_outputs(
    outputs: Any,
    *,
    orig_w: int,
    orig_h: int,
    orientation: int = 0,
) -> list[RawDetection]:
    """Chuyển kết quả dt_polys / dt_scores từ TextDetection.predict() về RawDetection tọa độ ảnh gốc."""
    if outputs is None:
        return []
    items = list(outputs) if not isinstance(outputs, dict) else [outputs]
    detections: list[RawDetection] = []
    for item in items:
        polys, scores = _extract_polys_and_scores(item)
        if polys is None:
            continue
        poly_list = list(polys)
        score_list = list(scores) if scores is not None else [1.0] * len(poly_list)
        for idx, poly in enumerate(poly_list):
            score = float(score_list[idx]) if idx < len(score_list) else 1.0
            pts = [[float(p[0]), float(p[1])] for p in poly]
            if len(pts) < 3:
                continue
            orig_poly = view_to_orig_poly(pts, orig_w, orig_h, orientation)
            bbox = clamp_bbox(poly_to_bbox(orig_poly), orig_w, orig_h)
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            detections.append(
                RawDetection(
                    id=new_region_id(),
                    poly=orig_poly,
                    bbox=bbox,
                    score=score,
                )
            )
    return detections
