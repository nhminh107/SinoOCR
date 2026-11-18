import argparse
import re
from pathlib import Path

import cv2

# Chỉ xử lý ảnh gốc dạng page<N>.png, bỏ qua ảnh đã chia (page<N>_1.png, page<N>_2.png).
ORIGINAL_PATTERN = re.compile(r"^page\d+\.png$", re.IGNORECASE)


def split_image(image, overlap_ratio=0.01):
    """Chia đôi ảnh theo chiều ngang, hai nửa chồng lên nhau một dải nhỏ ở giữa.

    Trả về (left, right). Dải overlap rộng overlap_ratio * width, chia đều cho hai bên.
    """
    if not 0 <= overlap_ratio < 1:
        raise ValueError("Cần 0 <= overlap_ratio < 1.")
    width = image.shape[1]
    mid = width // 2
    half_overlap = int(width * overlap_ratio) // 2
    left = image[:, : mid + half_overlap]
    right = image[:, mid - half_overlap :]
    return left, right


def split_wide_images(
    input_dir, max_width=4000, overlap_ratio=0.01, delete_original=False
):
    """Duyệt thư mục, ảnh có width > max_width thì chia đôi thành _1 (trái) và _2 (phải).

    Ví dụ page1.png -> page1_1.png, page1_2.png (lưu cùng thư mục).
    Trả về danh sách đường dẫn các ảnh đã được chia.
    """
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Không tìm thấy thư mục: {input_dir}")

    split_paths = []
    for path in sorted(input_dir.iterdir()):
        if not ORIGINAL_PATTERN.match(path.name):
            continue

        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"Không đọc được ảnh: {path}")
        if image.shape[1] <= max_width:
            continue

        left, right = split_image(image, overlap_ratio)
        for suffix, part in (("_1", left), ("_2", right)):
            out_path = path.with_name(f"{path.stem}{suffix}{path.suffix}")
            if not cv2.imwrite(str(out_path), part):
                raise OSError(f"Không lưu được ảnh: {out_path}")
        if delete_original:
            path.unlink()
        split_paths.append(path)
        print(f"Đã chia {path.name} (width={image.shape[1]})")
    return split_paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chia đôi các ảnh có width quá lớn.")
    parser.add_argument(
        "--input-dir",
        default="data/01_QuocTrieuHuongKhoaLuc",
        help="Thư mục chứa ảnh (mặc định: data/01_QuocTrieuHuongKhoaLuc).",
    )
    parser.add_argument("--max-width", type=int, default=4000)
    parser.add_argument(
        "--overlap-ratio", type=float, default=0.01,
        help="Tỉ lệ vùng chồng lên nhau so với width gốc (mặc định 0.01).",
    )
    parser.add_argument(
        "--delete-original", action="store_true",
        help="Xóa ảnh gốc sau khi chia.",
    )
    args = parser.parse_args()

    done = split_wide_images(
        args.input_dir, args.max_width, args.overlap_ratio, args.delete_original
    )
    print(f"Tổng số ảnh đã chia: {len(done)}")
