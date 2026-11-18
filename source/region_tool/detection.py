"""Wrapper suy luận cục bộ PP-OCRv6_medium_det kết hợp HisDoc1B Projection."""

from source.LayoutAnalysis.detection import (
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_NAME,
    SUPPORTED_ENGINES,
    DetectionBackendError,
    RegionDetector,
    parse_prediction_outputs,
)

__all__ = [
    "DEFAULT_MODEL_DIR",
    "DEFAULT_MODEL_NAME",
    "SUPPORTED_ENGINES",
    "DetectionBackendError",
    "RegionDetector",
    "parse_prediction_outputs",
]
