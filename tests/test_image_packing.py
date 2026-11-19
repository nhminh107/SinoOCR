import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from source.image_packing import (
    PackConfig,
    PageSpec,
    RegionItem,
    load_pages_from_layout_json,
    load_pages_from_regions_dir,
    pack_dataset,
    pack_image_regions,
)


class TestImagePacking(unittest.TestCase):
    def test_horizontal_rtl_order_and_spacing(self) -> None:
        img = Image.new("RGB", (400, 300), (240, 240, 240))
        regions = [
            RegionItem("r1", (250, 20, 350, 220), "title", 1),   # 100x200
            RegionItem("r2", (120, 40, 200, 190), "body", 2),    # 80x150
            RegionItem("r3", (10, 50, 70, 150), "body", 3),      # 60x100
        ]
        cfg = PackConfig(gap=20, padding=30, border_width=4, font_size=28)
        packed, placements = pack_image_regions(img, regions, config=cfg)

        self.assertIsNotNone(packed)
        self.assertEqual(len(placements), 3)
        self.assertEqual([p["index"] for p in placements], [1, 2, 3])
        self.assertEqual([p["id"] for p in placements], ["r1", "r2", "r3"])

        # Kiểm tra chiều ngang từ phải qua trái: box 1 bên phải nhất, box 3 bên trái nhất
        b1 = placements[0]["packed_bbox"]
        b2 = placements[1]["packed_bbox"]
        b3 = placements[2]["packed_bbox"]
        self.assertGreater(b1[0], b2[2])
        self.assertGreater(b2[0], b3[2])

        # Kiểm tra khoảng cách giữa các crop = gap + 2 * border_width
        expected_crop_gap = cfg.gap + 2 * cfg.border_width
        self.assertEqual(b1[0] - b2[2], expected_crop_gap)
        self.assertEqual(b2[0] - b3[2], expected_crop_gap)

        # Kiểm tra kích thước crop giữ nguyên
        self.assertEqual((b1[2] - b1[0], b1[3] - b1[1]), (100, 200))
        self.assertEqual((b2[2] - b2[0], b2[3] - b2[1]), (80, 150))
        self.assertEqual((b3[2] - b3[0], b3[3] - b3[1]), (60, 100))

    def test_empty_and_degenerate_regions(self) -> None:
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        packed, placements = pack_image_regions(img, [])
        self.assertIsNone(packed)
        self.assertEqual(placements, [])

        # Bbox ngoài biên hoặc rỗng bị loại
        bad_regions = [
            RegionItem("bad1", (250, 10, 300, 50), "body", 1),
            RegionItem("bad2", (50, 50, 50, 100), "body", 2),
        ]
        packed2, placements2 = pack_image_regions(img, bad_regions)
        self.assertIsNone(packed2)
        self.assertEqual(placements2, [])

    def test_max_width_wraps_rows_rtl(self) -> None:
        img = Image.new("RGB", (500, 200), (245, 245, 245))
        regions = [
            RegionItem("r1", (0, 0, 100, 100), "body", 1),
            RegionItem("r2", (100, 0, 200, 100), "body", 2),
            RegionItem("r3", (200, 0, 300, 100), "body", 3),
        ]
        cfg = PackConfig(gap=20, padding=20, border_width=2, max_width=280)
        packed, placements = pack_image_regions(img, regions, config=cfg)

        self.assertIsNotNone(packed)
        self.assertEqual(len(placements), 3)
        # Hàng 1 có r1 (phải) và r2 (trái); hàng 2 có r3 nằm bên dưới
        b1, b2, b3 = (p["packed_bbox"] for p in placements)
        self.assertEqual(b1[1], b2[1])
        self.assertGreater(b1[0], b2[2])
        self.assertGreater(b3[1], b1[3])

    def test_load_and_pack_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            ds = "01_QuocTrieuHuongKhoaLuc"
            (data_dir / ds).mkdir(parents=True)
            img_path = data_dir / ds / "page1_1.png"
            Image.new("RGB", (300, 200), (230, 230, 230)).save(img_path)

            # 1) Nguồn layout_all.json (thứ tự cố tình đảo để kiểm tra sort theo reading_order)
            layout_json = root / "layout_all.json"
            layout_json.write_text(
                json.dumps(
                    {
                        "pages": [
                            {
                                "relative_path": str(img_path),
                                "dataset": ds,
                                "filename": "page1_1.png",
                                "bboxes": [
                                    {"id": "b2", "bbox": [10, 10, 90, 150], "label": "body", "reading_order": 2},
                                    {"id": "b1", "bbox": [150, 10, 260, 150], "label": "title", "reading_order": 1},
                                    {"id": "b_ign", "bbox": [0, 0, 10, 10], "label": "ignore", "reading_order": 3},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pages = load_pages_from_layout_json(
                layout_json, data_dir=data_dir, exclude_labels={"ignore"}
            )
            self.assertEqual(len(pages), 1)
            self.assertEqual([r.region_id for r in pages[0].regions], ["b1", "b2"])

            out_dir = root / "out_layout"
            manifest = pack_dataset(pages, out_dir)
            self.assertEqual(manifest["summary"]["packed"], 1)
            self.assertTrue((out_dir / ds / "page1_1.png").is_file())
            self.assertTrue((out_dir / "manifest.json").is_file())

            # 2) Nguồn outputs/regions/<dataset>/<stem>.json
            regions_dir = root / "regions"
            (regions_dir / ds).mkdir(parents=True)
            (regions_dir / ds / "page1_1.json").write_text(
                json.dumps(
                    {
                        "dataset": ds,
                        "filename": "page1_1.png",
                        "image_path": str(img_path),
                        "regions": [
                            {"id": "u2", "bbox": [10, 10, 90, 150], "label": "body", "reading_order": 2},
                            {"id": "u1", "bbox": [150, 10, 260, 150], "label": "title", "reading_order": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            r_pages = load_pages_from_regions_dir(regions_dir, data_dir=data_dir, datasets=[ds])
            self.assertEqual(len(r_pages), 1)
            self.assertEqual([r.region_id for r in r_pages[0].regions], ["u1", "u2"])


    def test_rtl_stack_vertical_and_horizontal(self) -> None:
        # Trang dọc có 2 ô ngắn liên tiếp + 2 cột dài -> rtl_stack ghép gọn hơn rtl_row
        img = Image.new("RGB", (600, 1000), (245, 245, 245))
        regions = [
            RegionItem("r1", (400, 10, 520, 210), "title", 1),   # 120x200
            RegionItem("r2", (400, 220, 520, 520), "body", 2),   # 120x300
            RegionItem("r3", (220, 10, 340, 810), "body", 3),    # 120x800
            RegionItem("r4", (50, 10, 170, 810), "body", 4),     # 120x800
        ]
        row_img, _ = pack_image_regions(img, regions, PackConfig(mode="rtl_row"))
        stack_img, placements = pack_image_regions(img, regions, PackConfig(mode="rtl_stack"))

        self.assertIsNotNone(row_img)
        self.assertIsNotNone(stack_img)
        self.assertEqual([p["index"] for p in placements], [1, 2, 3, 4])
        # r1 và r2 xếp chồng cùng cột bên phải, r3 và r4 sang trái -> diện tích nhỏ hơn rtl_row
        self.assertLess(stack_img.width * stack_img.height, row_img.width * row_img.height)
        self._assert_no_overlap(placements)

        # Trang ngang có 8 dòng rộng -> rtl_stack không tạo dải siêu dài
        h_img = Image.new("RGB", (1200, 1000), (245, 245, 245))
        h_regions = [
            RegionItem(f"h{i}", (20, i * 110, 1020, i * 110 + 90), "body", i + 1)
            for i in range(8)
        ]
        h_row, _ = pack_image_regions(h_img, h_regions, PackConfig(mode="rtl_row"))
        h_stack, h_placements = pack_image_regions(h_img, h_regions, PackConfig(mode="rtl_stack"))
        self.assertIsNotNone(h_row)
        self.assertIsNotNone(h_stack)
        self.assertLess(h_stack.width, h_row.width // 2)
        self._assert_no_overlap(h_placements)

    def test_maxrects_no_overlap_and_compact(self) -> None:
        img = Image.new("RGB", (1200, 1200), (240, 240, 240))
        regions = [
            RegionItem("a", (0, 0, 150, 900), "body", 1),
            RegionItem("b", (160, 0, 310, 250), "title", 2),
            RegionItem("c", (320, 0, 470, 880), "body", 3),
            RegionItem("d", (480, 0, 630, 300), "body", 4),
            RegionItem("e", (640, 0, 790, 260), "body", 5),
            RegionItem("f", (800, 0, 950, 850), "body", 6),
        ]
        row_img, _ = pack_image_regions(img, regions, PackConfig(mode="rtl_row"))
        mr_img, placements = pack_image_regions(img, regions, PackConfig(mode="maxrects"))

        self.assertIsNotNone(row_img)
        self.assertIsNotNone(mr_img)
        self.assertEqual([p["index"] for p in placements], [1, 2, 3, 4, 5, 6])
        self.assertLess(mr_img.width * mr_img.height, row_img.width * row_img.height)
        self._assert_no_overlap(placements)

    def _assert_no_overlap(self, placements: list[dict]) -> None:
        boxes = [p["packed_bbox"] for p in placements]
        for i in range(len(boxes)):
            ax1, ay1, ax2, ay2 = boxes[i]
            self.assertGreater(ax2, ax1)
            self.assertGreater(ay2, ay1)
            for j in range(i + 1, len(boxes)):
                bx1, by1, bx2, by2 = boxes[j]
                overlaps = not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)
                self.assertFalse(overlaps, f"Boxes {i+1} và {j+1} bị chồng lấn: {boxes[i]} vs {boxes[j]}")


if __name__ == "__main__":
    unittest.main()
