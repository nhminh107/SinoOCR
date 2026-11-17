import argparse
import io
import re
import shutil
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FOLDERS = [
    PROJECT_DIR / "data/01_QuocTrieuHuongKhoaLuc",
    PROJECT_DIR / "data/02_TuLiemDaiKhoaLuc",
]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def collect_images(folders):
    images = []
    for folder in folders:
        paths = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES]
        images.extend(sorted(paths, key=lambda p: [
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", p.name)
        ]))
    return images


def replace_image_bytes(path, data):
    """Chỉ thay ảnh hiện có sau khi file mới đã ghi xong."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".crop-", suffix=path.suffix, delete=False) as file:
        temporary_path = Path(file.name)
    try:
        temporary_path.write_bytes(data)
        temporary_path.chmod(path.stat().st_mode & 0o777)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def save_crop(path, box, backup_dir):
    backup = backup_dir / path.parent.name / path.name
    if backup.resolve() == path.resolve():
        raise ValueError("Thư mục backup phải khác nơi lưu ảnh gốc.")
    with Image.open(path) as image:
        left, top, right, bottom = box
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError("Vùng crop nằm ngoài ảnh hoặc có kích thước bằng 0.")
        buffer = io.BytesIO()
        image.crop(box).save(buffer, format=image.format)

    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(path, backup)
    replace_image_bytes(path, buffer.getvalue())
    return backup


class CropTool:
    def __init__(self, root, paths, backup_dir):
        self.root, self.paths, self.backup_dir = root, paths, backup_dir
        self.index = 0
        self.image = self.photo = self.box = self.last_saved = None
        self.offset_x = self.offset_y = 0
        self.scale_x = self.scale_y = 1
        self.resize_job = None

        root.title("Crop ảnh — Enter để lưu đè và chuyển tiếp")
        width = min(1200, root.winfo_screenwidth() - 80)
        height = min(900, root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}")
        self.title = ttk.Label(root, padding=8)
        self.title.pack(fill="x")
        ttk.Label(root, text="Kéo chuột chọn vùng giữ lại · Enter: lưu & tiếp · Space/→: bỏ qua · ←: quay lại · Esc: bỏ chọn · Ctrl+Z: khôi phục ảnh vừa lưu", padding=5).pack(fill="x")
        self.canvas = tk.Canvas(root, background="#252525", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.status = ttk.Label(root, padding=8)
        self.status.pack(fill="x")
        buttons = ttk.Frame(root, padding=5)
        buttons.pack(fill="x")
        for text, command in [
            ("← Ảnh trước", lambda: self.move(-1)),
            ("Bỏ chọn", self.clear_selection),
            ("Lưu đè & tiếp (Enter)", self.save),
            ("Bỏ qua →", lambda: self.move(1)),
            ("Khôi phục ảnh vừa lưu", self.restore),
            ("Thoát", root.destroy),
        ]:
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=3)

        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<ButtonPress-1>", self.start_selection)
        self.canvas.bind("<B1-Motion>", self.update_selection)
        self.canvas.bind("<ButtonRelease-1>", self.update_selection)
        for key, command in [
            ("<Return>", self.save), ("<KP_Enter>", self.save),
            ("<space>", lambda: self.move(1)), ("<Right>", lambda: self.move(1)),
            ("<Left>", lambda: self.move(-1)), ("<Escape>", self.clear_selection),
            ("<Control-z>", self.restore),
        ]:
            root.bind(key, lambda event, action=command: (action(), "break")[1])
        self.load_image()

    def load_image(self):
        self.box = None
        if self.index == len(self.paths):
            self.image = self.photo = None
            self.canvas.delete("all")
            self.title.config(text=f"Đã duyệt hết {len(self.paths)} ảnh trong các thư mục.")
            self.status.config(text="Có thể quay lại bằng ← hoặc khôi phục ảnh vừa lưu bằng Ctrl+Z.")
            return
        path = self.paths[self.index]
        try:
            with Image.open(path) as image:
                self.image = image.copy()
        except OSError as error:
            self.image = self.photo = None
            self.canvas.delete("all")
            self.title.config(text=f"Không đọc được: {path}")
            self.status.config(text=f"{error} — nhấn Space để bỏ qua.")
            return
        self.title.config(text=f"{self.index + 1}/{len(self.paths)} · {path.parent.name}/{path.name} · {self.image.width} × {self.image.height} px")
        self.status.config(text="Kéo chuột để chọn hình chữ nhật cần giữ lại.")
        self.draw_image()

    def on_resize(self, event):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(80, self.draw_image)

    def draw_image(self):
        self.resize_job = None
        if self.image is None:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 2 or height < 2:
            return
        scale = min(width / self.image.width, height / self.image.height)
        size = (max(1, round(self.image.width * scale)), max(1, round(self.image.height * scale)))
        self.scale_x, self.scale_y = size[0] / self.image.width, size[1] / self.image.height
        self.offset_x, self.offset_y = (width - size[0]) // 2, (height - size[1]) // 2
        self.photo = ImageTk.PhotoImage(self.image.resize(size, Image.Resampling.BILINEAR))
        self.canvas.delete("all")
        self.canvas.create_image(self.offset_x, self.offset_y, image=self.photo, anchor="nw")
        self.draw_selection()

    def image_point(self, event):
        x = round((event.x - self.offset_x) / self.scale_x)
        y = round((event.y - self.offset_y) / self.scale_y)
        return max(0, min(self.image.width, x)), max(0, min(self.image.height, y))

    def start_selection(self, event):
        if self.image is None:
            return
        self.canvas.focus_set()
        self.start = self.image_point(event)
        self.box = (*self.start, *self.start)
        self.draw_selection()

    def update_selection(self, event):
        if self.image is None or self.box is None:
            return
        x, y = self.image_point(event)
        x0, y0 = self.start
        self.box = (min(x0, x), min(y0, y), max(x0, x), max(y0, y))
        self.draw_selection()

    def draw_selection(self):
        self.canvas.delete("selection")
        if self.box is None:
            return
        left, top, right, bottom = self.box
        self.canvas.create_rectangle(
            self.offset_x + left * self.scale_x, self.offset_y + top * self.scale_y,
            self.offset_x + right * self.scale_x, self.offset_y + bottom * self.scale_y,
            outline="#00ff9d", width=2, tags="selection",
        )
        self.status.config(text=f"Giữ: x={left}:{right}, y={top}:{bottom} · Kết quả: {right-left} × {bottom-top} px · Enter để lưu đè.")

    def clear_selection(self):
        self.box = None
        self.canvas.delete("selection")
        self.status.config(text="Đã bỏ vùng chọn. Kéo chuột để chọn lại.")

    def move(self, step):
        self.index = max(0, min(len(self.paths), self.index + step))
        self.load_image()

    def save(self):
        if self.image is None or self.box is None:
            self.status.config(text="Chọn vùng giữ lại trước khi nhấn Enter; Space để bỏ qua ảnh.")
            return
        path = self.paths[self.index]
        try:
            backup = save_crop(path, self.box, self.backup_dir)
        except (OSError, ValueError) as error:
            messagebox.showerror("Chưa lưu được ảnh", str(error), parent=self.root)
            return
        self.last_saved = (self.index, backup)
        self.move(1)

    def restore(self):
        if self.last_saved is None:
            self.status.config(text="Chưa có ảnh vừa lưu để khôi phục.")
            return
        index, backup = self.last_saved
        try:
            replace_image_bytes(self.paths[index], backup.read_bytes())
        except OSError as error:
            messagebox.showerror("Không khôi phục được", str(error), parent=self.root)
            return
        self.index, self.last_saved = index, None
        self.load_image()
        self.status.config(text="Đã khôi phục ảnh gốc từ backup. Có thể chọn vùng crop lại.")


def main():
    parser = argparse.ArgumentParser(description="Crop lần lượt toàn bộ ảnh của hai folder; Enter lưu đè rồi chuyển tiếp.")
    parser.add_argument("folders", nargs="*", type=Path, default=DEFAULT_FOLDERS)
    parser.add_argument("--backup-dir", type=Path, default=PROJECT_DIR / "crop_backups")
    args = parser.parse_args()
    paths = collect_images(args.folders)
    if not paths:
        parser.error("Không tìm thấy ảnh trong các thư mục đã chọn.")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        parser.exit(1, f"Không mở được cửa sổ: {error}\nHãy chạy tool từ terminal trên desktop có màn hình.\n")
    CropTool(root, paths, args.backup_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
