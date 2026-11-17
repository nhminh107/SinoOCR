# Công cụ Gán nhãn Vùng OCR Cục bộ (Sino-Nom / Tiếng Việt)

Ứng dụng desktop cục bộ bằng **Tkinter + Pillow (`tk.Canvas`)** phục vụ phát hiện vùng chữ bằng **PP-OCRv6** (`PP-OCRv6_medium_det` qua `paddle` trên `gpu:0`), gom nhóm thành cột dọc / dòng ngang theo góc nhìn hiện tại, chỉnh sửa thủ công, đánh số thứ tự đọc hàng loạt và lưu JSON kèm thông tin nguồn gốc (provenance).

---

## 1. Lệnh chạy chuẩn trong môi trường `OCR_Env`

Chạy từ thư mục gốc dự án `/home/nhminh/AI_Project/NLP_OCR_Sino`:

```bash
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool
```

Tuỳ chọn dòng lệnh:

```bash
# Chỉ định thư mục ảnh cụ thể và thư mục lưu annotation
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool \
  data/01_QuocTrieuHuongKhoaLuc data/02_TuLiemDaiKhoaLuc \
  --output-dir outputs/regions

# Xem trợ giúp CLI (không khởi tạo Tkinter hay load model)
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool --help
```

Chạy kiểm thử (`unittest` thư viện chuẩn):

```bash
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m unittest tests/test_region_tool.py
```

---

## 2. Tính năng chính

- **Duyệt dữ liệu theo thứ tự tự nhiên**: Mặc định quét `data/01_QuocTrieuHuongKhoaLuc` (215 PNG) và `data/02_TuLiemDaiKhoaLuc` (29 PNG), sắp xếp tự nhiên (`page2.png` trước `page10.png`). Ảnh gốc trong `data/` luôn bất biến.
- **Xoay & Thu phóng mượt**: Pan/Zoom, vừa khung (`F`), xoay góc nhìn `-90°` / `+90°` (`[` / `]`, hữu ích cho các trang xoay như `page110`), nhưng mọi toạ độ `bbox` lưu trong JSON luôn giữ nguyên theo hệ toạ độ pixel ảnh gốc.
- **Phát hiện vùng cục bộ trên trang hiện tại**:
  - Model được nạp lười (lazy-load) trong thread nền khi bấm **Nhận diện vùng**; không tự động OCR hàng loạt 244 ảnh.
  - Ảnh đầu vào được chuyển từ RGB sang **BGR** trước khi đưa vào `TextDetection.predict()` của PaddleX/OpenCV.
  - Vô hiệu hoá các thao tác xung đột trong lúc suy luận; hiển thị lỗi rõ ràng trên UI mà không làm mất các chỉnh sửa đang có.
  - Nếu trang đã có vùng gán nhãn, ứng dụng hỏi **Thay thế / Giữ vùng cũ / Hủy**; `raw_detections` luôn được lưu tách biệt để có thể **Gom lại cột/dòng** mà không cần chạy lại model.
- **Gom nhóm cột / dòng theo góc nhìn hiện tại**:
  - **Cột dọc (Chữ Hán - mặc định)**: Gom theo trục dọc trên góc nhìn đã xoay (nếu có) rồi quy đổi `bbox` về tọa độ ảnh gốc; giới hạn bề rộng cột hợp nhất để tránh gộp nhầm cột kề nhau hoặc bị các box tiêu đề/đầu trang rộng bắc cầu nối hai cột nội dung.
  - **Dòng ngang (Quốc ngữ)**: Giới hạn khoảng trống theo trục X tương đối với chiều cao chữ cục bộ để không gộp nhầm chữ cùng hàng Y qua khe giữa 2 trang sách đôi hoặc 2 cột tách biệt.
  - Lưu ý: Công cụ không chạy layout model nhận diện tiêu đề tự động; nhãn `title`, `header`, `footer`, `ignore` được chỉnh sửa thủ công.
- **Chỉnh sửa vùng & Undo/Redo**:
  - Vẽ box mới, chọn đơn / `Shift+Click` / kéo khung chọn nhiều vùng (marquee), di chuyển, kéo 8 nút góc/cạnh để đổi kích thước, gộp (`M`), chia ngang/dọc (tại trung điểm hoặc tại điểm click), xoá (`Delete`), Hoàn tác (`Ctrl+Z`) / Làm lại (`Ctrl+Y`).
  - Thuộc tính áp dụng cho vùng đang chọn: Nhãn (`body`/`title`/`header`/`footer`/`ignore`), Ngôn ngữ (`han`/`viet`/`unknown`), Vai trò (`original`/`phonetic`/`translation`/`other`), Hướng chữ (`vertical`/`horizontal`), Thứ tự đọc, Trạng thái duyệt vùng (`pending`/`reviewed`/`needs_review`).
  - Khi chọn nhiều vùng có thuộc tính khác nhau, giá trị `— Giữ nguyên —` bảo toàn thuộc tính từng vùng. Thứ tự và ghi chú riêng chỉ sửa trong bảng thuộc tính khi chọn một vùng.
  - Bảng bên phải có thanh cuộn để dùng được trên màn hình nhỏ; khi zoom chỉ render phần ảnh đang nhìn thấy để hạn chế RAM.
- **Đánh số thứ tự đọc hàng loạt (Bulk Numbering)**:
  - Kéo khung chọn nhiều box (chế độ **Đánh số marquee** hoặc chọn rồi bấm **Đánh số vùng đang chọn**), chọn số bắt đầu và chiều đọc (`Cột phải → trái`, `Cột trái → phải`, `Dòng trên → dưới`) tính theo góc nhìn hiện tại.
  - Chỉ đánh số lại các vùng được chọn một cách tất định, giữ nguyên thứ tự tương đối của các vùng còn lại và đảm bảo mọi `reading_order` đều là số nguyên dương duy nhất toàn cục.
- **An toàn dữ liệu & Xuất ảnh crop**:
  - Nếu file JSON hiện có bị hỏng, ứng dụng báo lỗi rõ ràng và **không** tự động ghi đè bằng trạng thái rỗng.
  - Nếu kích thước ảnh thay đổi so với annotation đã lưu, cần kiểm tra lại tọa độ; ứng dụng báo lỗi thay vì âm thầm dùng bbox cũ.
  - Xuất các vùng đã chọn hoặc toàn bộ vùng nội dung (giữ `title`, loại trừ `ignore`/`header`/`footer`) ở độ phân giải gốc kèm `manifest.json`.

---

## 3. Điều khiển & Phím tắt

| Phím tắt / Thao tác | Chức năng |
|---|---|
| `PgUp` / `PgDown` | Chuyển trang trước / trang tiếp (tự động lưu nếu có thay đổi) |
| `Ctrl+S` | Lưu annotation JSON của trang hiện tại |
| `Ctrl+E` | Xuất ảnh crop & `manifest.json` |
| `Ctrl+Z` / `Ctrl+Y` | Hoàn tác / Làm lại thao tác chỉnh sửa vùng |
| `V` / `N` / `B` | Chế độ Chọn-Sửa / Vẽ vùng mới / Đánh số marquee |
| `F` | Vừa khung (Fit view) |
| `[` / `]` | Xoay góc nhìn `-90°` / `+90°` |
| `M` | Gộp các vùng đang chọn thành 1 khung bao |
| `Delete` | Xoá các vùng đang chọn |
| Lăn chuột / `+` `-` | Phóng to / Thu nhỏ |
| Giữ chuột giữa (hoặc chế độ Pan) | Kéo di chuyển vùng nhìn ảnh |

---

## 4. Định dạng JSON & Thông tin Nguồn gốc (Provenance)

Mỗi ảnh được lưu tại `outputs/regions/<dataset>/<image_stem>.json`, cùng tệp phiên làm việc `outputs/regions/session.json` để mở lại đúng ảnh gần nhất.

- `image_width`, `image_height`: Kích thước ảnh gốc (pixel).
- `orientation`: Góc xoay hiển thị (`0`, `90`, `180`, `270`).
- `source.page_index`, `source.image_part`: Tách từ tên tệp (`page12_1.png` → `page_index: 12`, `image_part: 1`). **Lưu ý**: `_1` / `_2` là phần cắt vật lý của ảnh, **không phải** nhãn ngôn ngữ Hán/Việt.
- `source.pdf_basename`, `source.pdf_page_range`, `source.pdf_page_index`: Ánh xạ theo chỉ số trang trong tệp PDF (`_trang_0331-0440`, `_trang_0090-0118`), **không** đồng nhất với số trang in trên sách cổ.
- `raw_detections`: Danh sách polygon, `bbox` và `score` gốc từ `PP-OCRv6_medium_det` (tách biệt hoàn toàn với `regions`).
- `regions`: Danh sách vùng đã hiệu chỉnh gồm `id` ổn định, `bbox` theo toạ độ ảnh gốc, nhãn, ngôn ngữ, vai trò, hướng chữ, `reading_order`, `review_state`, `notes`.

---

## 5. Cấu hình Model Cục bộ

- Mặc định sử dụng backend Paddle GPU trong `OCR_Env` (`paddlepaddle-gpu 3.3.1`, `paddleocr 3.7.0`, `paddlex 3.7.2`), **không** tự động fallback về CPU hay thay thế bằng model OCR khác:
  ```python
  from paddleocr import TextDetection
  model = TextDetection(
      model_name="PP-OCRv6_medium_det",
      model_dir="/home/nhminh/.paddlex/official_models/PP-OCRv6_medium_det",
      device="gpu:0",
  )
  ```
- Dùng model cục bộ, tắt bước kiểm tra nguồn tải model qua mạng. Cache riêng của PaddleX đặt trong `outputs/regions/.cache/paddlex` (hoặc theo `PADDLE_PDX_CACHE_HOME` nếu đã cấu hình).

---

## 6. Phạm vi Giới hạn của Module

Module này chỉ thực hiện **phát hiện vùng chữ hình học (text region detection)**, gom nhóm cột/dòng và gán nhãn cấu trúc/thứ tự đọc.

- **Không** thực hiện nhận dạng chữ (transcription/OCR text), dịch nghĩa, hay xuất bảng đối chiếu TSV trong module này.
- Việc **đối chiếu ngữ nghĩa / căn hàng (semantic alignment)** giữa các đoạn văn bản Hán – Việt (vốn chưa có ánh xạ 1-1 sẵn) sẽ được thực hiện ở các bước xử lý sau trên nền JSON và ảnh crop đã xuất từ công cụ này.
