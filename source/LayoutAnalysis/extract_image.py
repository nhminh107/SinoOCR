import fitz
from PIL import Image
import io
from pathlib import Path

def extract_img(pdf_name, pdf_path, start_page, end_page):
    output_path = Path.cwd() / "data" / f"{pdf_name}"
    output_path.mkdir(exist_ok=True)
    with fitz.open(pdf_path) as doc:
        for i in range (0, len(doc)):
            page = doc[i]
            pix = page.get_pixmap(dpi=400)
            pix.save(output_path / f"page{i+1}.png")


data_path = Path.cwd() / "data"
pdf_1 = data_path / "01_QuốcTriềuHươngKhoaLục_國朝鄉科錄_trang_0331-0440.pdf"
pdf_2 = data_path / "02_Từ Liêm đại khoa lục_trang_0090-0118.pdf"

extract_img("01_QuocTrieuHuongKhoaLuc", pdf_1, 331, 440)
extract_img("02_TuLiemDaiKhoaLuc", pdf_2, 90,118)

