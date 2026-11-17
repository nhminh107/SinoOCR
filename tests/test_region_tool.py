"""Kiểm thử đơn vị (stdlib unittest) và Tk smoke test cho source.region_tool."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from source.region_tool.detection import (
    DetectionBackendError,
    RegionDetector,
    parse_prediction_outputs,
)
from source.region_tool.geometry import (
    DEFAULT_FOLDERS,
    RawDetection,
    Region,
    bulk_number_regions,
    collect_images,
    group_detections,
    merge_regions,
    orig_to_view_bbox,
    parse_image_filename,
    split_region,
    view_to_orig_bbox,
)
from source.region_tool.persistence import (
    annotation_path_for,
    export_crops,
    load_annotation,
    load_session,
    new_annotation_for_entry,
    save_annotation,
    save_session,
)


class TestRegionTool(unittest.TestCase):
    def test_natural_ordering_and_metadata_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ds_dir = tmp_path / "01_SampleDataset"
            ds_dir.mkdir()
            (tmp_path / "01_Sample_trang_0331-0440.pdf").write_bytes(b"%PDF-1.4")
            for name in ("page10_1.png", "page2_2.png", "page2_1.png", "page1.png", "page9.png"):
                (ds_dir / name).write_bytes(b"fake")

            entries = collect_images([ds_dir])
            self.assertEqual(
                [e.filename for e in entries],
                ["page1.png", "page2_1.png", "page2_2.png", "page9.png", "page10_1.png"],
            )
            self.assertEqual(entries[1].page_index, 2)
            self.assertEqual(entries[1].image_part, 1)
            self.assertIsNotNone(entries[1].pdf_source)
            self.assertEqual(entries[1].pdf_source.pdf_basename, "01_Sample_trang_0331-0440.pdf")
            self.assertEqual(entries[1].pdf_page_index, 332)
            self.assertEqual(parse_image_filename("page110_2"), (110, 2))
            self.assertEqual(parse_image_filename("other_name"), (None, None))

    def test_default_datasets_inventory_if_present(self) -> None:
        if not all(folder.is_dir() for folder in DEFAULT_FOLDERS):
            self.skipTest("Thư mục data/ mặc định không có sẵn.")
        entries = collect_images(DEFAULT_FOLDERS)
        self.assertEqual(len(entries), 244)
        first_ds = [e for e in entries if e.dataset == "01_QuocTrieuHuongKhoaLuc"]
        second_ds = [e for e in entries if e.dataset == "02_TuLiemDaiKhoaLuc"]
        self.assertEqual(len(first_ds), 215)
        self.assertEqual(len(second_ds), 29)
        self.assertEqual(first_ds[0].filename, "page1_1.png")
        self.assertEqual(second_ds[0].filename, "page1.png")

    def test_vertical_column_grouping_avoids_adjacent_merge_and_wide_title_bridge(self) -> None:
        # Hai cột cạnh nhau (cách 4px) + 1 box tiêu đề rộng nằm phía trên cả 2 cột
        dets = [
            RawDetection("title", [], [95, 10, 190, 38], 0.95),
            RawDetection("d1", [], [100, 50, 140, 120], 0.95),
            RawDetection("d2", [], [102, 130, 138, 210], 0.92),
            RawDetection("d3", [], [144, 55, 184, 125], 0.96),
            RawDetection("d4", [], [146, 135, 182, 215], 0.91),
        ]
        regions = group_detections(
            dets,
            mode="vertical_columns",
            overlap_threshold=0.35,
            reading_order="rtl_ttb",
        )
        # Tiêu đề rộng không được bắc cầu gộp 2 cột nội dung lại thành 1
        self.assertEqual(len(regions), 3)
        body_cols = [r for r in regions if r.bbox[1] >= 45]
        self.assertEqual(len(body_cols), 2)

    def test_horizontal_grouping_limits_x_gap_across_columns_or_spread(self) -> None:
        # Hai trang sách đôi cùng hàng Y nhưng cách nhau một khoảng X lớn (120px)
        dets = [
            RawDetection("l1", [], [40, 100, 160, 124], 0.9),
            RawDetection("l2", [], [175, 101, 290, 123], 0.9),
            RawDetection("r1", [], [410, 100, 540, 124], 0.9),
        ]
        regions = group_detections(dets, mode="horizontal_lines")
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0].bbox, [40, 100, 290, 124])
        self.assertEqual(regions[1].bbox, [410, 100, 540, 124])

    def test_rotated_view_grouping_and_bulk_numbering_keep_original_coords(self) -> None:
        # Ảnh gốc 1000x600 bị xoay 90° CW -> trong view xoay (600x1000) là 2 cột dọc
        orig_w, orig_h = 1000, 600
        # Trong ảnh gốc (chưa xoay), hai cột dọc đứng khi xoay 90° CW thực chất nằm ngang
        dets = [
            RawDetection("a1", [], [50, 100, 120, 140], 0.95),
            RawDetection("a2", [], [130, 102, 210, 138], 0.95),
            RawDetection("b1", [], [50, 160, 120, 200], 0.95),
            RawDetection("b2", [], [130, 162, 210, 198], 0.95),
        ]
        regions = group_detections(
            dets,
            mode="vertical_columns",
            reading_order="rtl_ttb",
            orig_w=orig_w,
            orig_h=orig_h,
            orientation=90,
        )
        self.assertEqual(len(regions), 2)
        # Tọa độ lưu trữ vẫn nằm trong khung ảnh gốc 1000x600
        for reg in regions:
            self.assertGreaterEqual(reg.bbox[0], 0)
            self.assertLessEqual(reg.bbox[2], orig_w)
            self.assertLessEqual(reg.bbox[3], orig_h)

        # Đánh số lại với orientation=90 theo rtl_ttb
        numbered = bulk_number_regions(
            regions,
            {r.id for r in regions},
            start_index=1,
            order_mode="rtl_ttb",
            orig_w=orig_w,
            orig_h=orig_h,
            orientation=90,
        )
        orders = [r.reading_order for r in numbered]
        self.assertEqual(orders, [1, 2])

    def test_bulk_numbering_selected_only_and_unique(self) -> None:
        regions = [
            Region(id="r1", bbox=[300, 50, 340, 200], label="body", reading_order=1),
            Region(id="r2", bbox=[200, 50, 240, 200], label="body", reading_order=2),
            Region(id="r3", bbox=[100, 50, 140, 200], label="body", reading_order=3),
            Region(id="r_ign", bbox=[150, 10, 250, 35], label="header", reading_order=4),
        ]
        updated = bulk_number_regions(
            regions,
            {"r2", "r3", "r_ign"},
            start_index=2,
            order_mode="rtl_ttb",
            exclude_non_content=True,
        )
        by_id = {r.id: r for r in updated}
        self.assertEqual(by_id["r2"].reading_order, 2)
        self.assertEqual(by_id["r3"].reading_order, 3)
        self.assertEqual(by_id["r1"].reading_order, 1)
        self.assertEqual(by_id["r_ign"].reading_order, 4)
        orders = [r.reading_order for r in updated]
        self.assertEqual(len(orders), len(set(orders)))

    def test_rotation_roundtrip_and_prediction_mapping(self) -> None:
        orig_w, orig_h = 1000, 600
        orig_box = [120.0, 80.0, 260.0, 420.0]
        for angle in (0, 90, 180, 270):
            vbox = orig_to_view_bbox(orig_box, orig_w, orig_h, angle)
            back = view_to_orig_bbox(vbox, orig_w, orig_h, angle)
            for a, b in zip(back, orig_box):
                self.assertAlmostEqual(a, b)

        mock_outputs = [{"dt_polys": [[[100, 50], [200, 50], [200, 300], [100, 300]]], "dt_scores": [0.98]}]
        dets = parse_prediction_outputs(mock_outputs, orig_w=orig_w, orig_h=orig_h, orientation=90)
        self.assertEqual(len(dets), 1)
        self.assertEqual(dets[0].bbox, [50.0, 400.0, 300.0, 500.0])

    def test_save_load_corrupt_json_and_crop_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ds_dir = tmp_path / "01_Test"
            ds_dir.mkdir()
            img_path = ds_dir / "page5_1.png"
            Image.new("RGB", (400, 300), color=(240, 235, 220)).save(img_path)

            entry = collect_images([ds_dir])[0]
            out_dir = tmp_path / "outputs" / "regions"
            ann = new_annotation_for_entry(entry, 400, 300)
            ann.orientation = 90
            ann.raw_detections = [
                RawDetection("raw1", [[10, 10], [80, 10], [80, 200], [10, 200]], [10, 10, 80, 200], 0.9)
            ]
            ann.regions = [
                Region(id="reg_a", bbox=[10, 10, 100, 220], label="title", reading_order=1, review_state="reviewed"),
                Region(id="reg_b", bbox=[120, 10, 200, 40], label="header", reading_order=2),
            ]
            save_annotation(out_dir, entry, ann)
            save_session(out_dir, entry, 0)

            loaded = load_annotation(out_dir, entry, 400, 300)
            self.assertEqual(loaded.orientation, 90)
            self.assertEqual(len(loaded.raw_detections), 1)
            self.assertEqual([r.id for r in loaded.regions], ["reg_a", "reg_b"])
            with self.assertRaisesRegex(ValueError, "Kích thước ảnh"):
                load_annotation(out_dir, entry, 800, 600)

            sess = load_session(out_dir)
            self.assertIsNotNone(sess)
            self.assertEqual(sess["last_image_key"], entry.key)

            manifest_path = export_crops(entry, loaded, out_dir / "crops", include_non_content=False)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["crops"]), 1)
            self.assertEqual(manifest["crops"][0]["region_id"], "reg_a")

            # JSON hỏng phải báo lỗi rõ ràng, không trả về trạng thái rỗng âm thầm
            annotation_path_for(out_dir, entry).write_text("{corrupt json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_annotation(out_dir, entry, 400, 300)

    def test_merge_and_split_regions(self) -> None:
        regs = [
            Region(id="a", bbox=[10, 10, 50, 100], reading_order=1),
            Region(id="b", bbox=[10, 110, 50, 200], reading_order=2),
        ]
        merged, merged_id = merge_regions(regs, {"a", "b"})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged_id, "a")
        self.assertEqual(merged[0].bbox, [10, 10, 50, 200])

        split_list, parts = split_region(merged, "a", axis="horizontal", split_coord=105.0)
        self.assertIsNotNone(parts)
        self.assertEqual(len(split_list), 2)
        self.assertEqual(split_list[0].bbox, [10, 10, 50, 105.0])
        self.assertEqual(split_list[1].bbox, [10, 105.0, 50, 200])

    def test_detector_converts_rgb_to_bgr_and_explicit_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            img_path = tmp_path / "rgb.png"
            # Tạo ảnh thuần đỏ RGB (255, 0, 0) -> BGR phải là (0, 0, 255)
            Image.new("RGB", (40, 30), color=(255, 0, 0)).save(img_path)

            captured: dict[str, np.ndarray] = {}

            class _FakeModel:
                def predict(self, arr: np.ndarray):
                    captured["arr"] = arr
                    return []

            det = RegionDetector(engine="paddle", device="gpu:0")
            with mock.patch.object(det, "ensure_loaded", return_value=_FakeModel()):
                det.detect_image(img_path, orientation=0)

            self.assertEqual(captured["arr"].shape, (30, 40, 3))
            self.assertEqual(tuple(int(v) for v in captured["arr"][0, 0]), (0, 0, 255))

            paddle_only = tmp_path / "PP-OCRv6_medium_det"
            paddle_only.mkdir()
            (paddle_only / "inference.json").write_text("{}", encoding="utf-8")
            (paddle_only / "inference.pdiparams").write_bytes(b"0")
            onnx_det = RegionDetector(model_dir=paddle_only, engine="onnxruntime")
            with self.assertRaises(DetectionBackendError):
                onnx_det.ensure_loaded()

    def test_cli_help_does_not_initialize_tk(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "source.region_tool", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("PP-OCRv6_medium_det", proc.stdout)
        self.assertIn("gpu:0", proc.stdout)

    def test_web_workspace_actions_save_and_source_integrity(self) -> None:
        from source.region_tool.server import RegionWorkspace
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ds_dir = tmp_path / "02_Smoke"
            ds_dir.mkdir()
            Image.new("RGB", (300, 200), color=(255, 255, 255)).save(ds_dir / "page1.png")
            entries = collect_images([ds_dir])
            original = entries[0].path.read_bytes()
            workspace = RegionWorkspace(entries, tmp_path / "out")
            annotation = workspace.load(0)
            annotation.regions = [
                Region("a", [10, 10, 50, 150], reading_order=1),
                Region("b", [100, 10, 140, 150], reading_order=2),
            ]
            result = workspace.action({
                "image_id": 0, "annotation": annotation.to_dict(), "action": "number",
                "selected_ids": ["a", "b"], "settings": {"start_index": 7},
            })
            self.assertEqual([r["reading_order"] for r in result["annotation"]["regions"]], [7, 8])
            workspace.save(0, result["annotation"])
            self.assertEqual(len(workspace.load(0).regions), 2)
            self.assertEqual(entries[0].path.read_bytes(), original)
            with self.assertRaises(ValueError):
                workspace.entry(-1)
            invalid = result["annotation"]
            invalid["regions"][0]["id"] = "../../escape"
            with self.assertRaisesRegex(ValueError, "Mã vùng"):
                workspace.save(0, invalid)
            self.assertEqual(len(workspace.load(0).regions), 2)

    def test_web_workspace_preserves_corrupt_annotation(self) -> None:
        from source.region_tool.server import RegionWorkspace
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample"
            folder.mkdir()
            Image.new("RGB", (100, 100)).save(folder / "page1.png")
            entries = collect_images([folder])
            workspace = RegionWorkspace(entries, Path(tmp) / "out")
            valid = workspace.load(0).to_dict()
            path = annotation_path_for(workspace.output_dir, entries[0])
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                workspace.save(0, valid)
            self.assertEqual(path.read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
