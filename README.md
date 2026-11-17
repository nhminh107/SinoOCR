# Sino Studio

Giao diện web cục bộ để duyệt và hiệu chỉnh vùng chữ Hán – Việt. Backend dùng model **PP-OCRv6_medium_det** trên **GPU** trong môi trường `OCR_Env`. Frontend dùng HTML, CSS và JavaScript thuần; không cần cài thêm framework.

## Chạy ứng dụng

Từ thư mục gốc dự án:

```bash
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool
```

Mở địa chỉ được in ra terminal, mặc định là `http://127.0.0.1:8765`. Ứng dụng cũng tự mở trình duyệt. Dừng bằng `Ctrl+C` trong terminal.

```bash
# Không tự mở trình duyệt hoặc dùng cổng khác
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool --no-browser --port 8766

# Chọn thư mục ảnh và nơi lưu kết quả
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m source.region_tool \
  data/01_QuocTrieuHuongKhoaLuc data/02_TuLiemDaiKhoaLuc \
  --output-dir outputs/regions
```

Mặc định duyệt **215 ảnh Quốc triều** và **29 ảnh Từ Liêm**, sắp xếp theo số trang tự nhiên. Ảnh nguồn luôn được giữ nguyên. Trên cửa sổ hẹp, dùng nút danh sách tài liệu và nút thuộc tính ở thanh đầu để mở hai bảng phụ.

## Quy trình sử dụng

1. Chọn ảnh trong danh sách. Chọn **Cột dọc** hoặc **Dòng ngang** ở tab **Đánh số & Gom**; xoay góc nhìn nếu cần.
2. Bấm **Nhận diện vùng**. Model chạy nền trên ảnh hiện tại. Kết quả thô và các vùng đã sửa được lưu riêng; nếu ảnh đã có vùng, chọn thay thế, giữ vùng cũ hoặc hủy.
3. Duyệt bbox: vẽ mới, kéo di chuyển, kéo các nút góc/cạnh để đổi kích thước, chia, gộp hoặc xóa. Có thể bật **Raw OCR** để đối chiếu vùng máy phát hiện.
4. Đánh số hàng loạt: đặt **Số bắt đầu** và **Chiều đọc**, chọn công cụ **Đánh số**, rồi kéo khung qua các bbox cần đánh số. Hoặc chọn nhiều vùng rồi bấm **Đánh số vùng chọn**. Số được tính theo góc nhìn hiện tại; các vùng còn lại giữ thứ tự tương đối và được điều chỉnh để không trùng số.
5. Gán nhãn **nội dung, tiêu đề, đầu trang, chân trang, bỏ qua**; ngôn ngữ, vai trò **nguyên bản / phiên âm / dịch nghĩa**, hướng chữ và trạng thái duyệt. Khi chọn nhiều vùng, **Giữ nguyên** bảo toàn các thuộc tính khác nhau. Ghi chú và thứ tự riêng sửa khi chọn một vùng.
6. Annotation tự lưu sau thao tác và trước khi chuyển ảnh; có nút **Lưu**. Tab **Trang & Xuất** cho phép ghi chú trang, đánh dấu đã duyệt và xuất crop kèm manifest.

Model phát hiện chữ và gom vùng theo hình học. **Tiêu đề được gán nhãn thủ công**, chưa chạy model phân loại bố cục. Đầu trang/chân trang/bỏ qua được loại khỏi xuất nội dung; tiêu đề vẫn được giữ.

## Thao tác nhanh

| Thao tác | Chức năng |
|---|---|
| `V` / `N` / `B` | Chọn-sửa / Vẽ vùng / Kéo khung đánh số |
| Click, `Shift+Click`, kéo khung từ chỗ trống | Chọn một hoặc nhiều vùng |
| Lăn chuột, `+` / `-`, `F` | Zoom tại con trỏ / zoom / vừa khung |
| Chuột giữa hoặc chế độ Kéo ảnh | Di chuyển vùng nhìn |
| `[` / `]` | Xoay góc nhìn ±90° |
| `M`, `Delete` | Gộp vùng chọn, xóa vùng chọn |
| `Ctrl+Z` / `Ctrl+Y` | Hoàn tác / làm lại chỉnh sửa vùng |
| `Ctrl+S` | Lưu, kể cả khi đang gõ ghi chú |
| `PgUp` / `PgDown` | Ảnh trước / tiếp |
| `?` | Trợ giúp phím tắt |

## Dữ liệu và truy vết

```text
source/region_tool/
  __main__.py       # CLI
  server.py         # API localhost, tác vụ GPU nền
  detection.py      # PP-OCRv6
  geometry.py       # Gom cột/dòng, xoay tọa độ, đánh số, chia/gộp
  persistence.py    # JSON, phiên làm việc, crop và manifest
  static/           # index.html, style.css, app.js, favicon.svg

outputs/regions/
  <dataset>/<image_stem>.json
  session.json
  crops/<dataset>/<image_stem>/
  .cache/paddlex/
```

JSON lưu kích thước ảnh, góc nhìn, mã trang/phần ảnh, PDF nguồn, trạng thái duyệt, ghi chú, `raw_detections` và `regions`. Mỗi vùng có ID ổn định, bbox, nhãn, ngôn ngữ, vai trò, hướng chữ, số thứ tự và trạng thái duyệt.

**Mọi tọa độ đều theo pixel ảnh gốc**, kể cả khi xoay hoặc zoom. `_1`/`_2` là phần ảnh vật lý, không phải Hán/Việt. Chỉ số PDF suy ra từ tên tệp không đồng nhất với số trang in trên sách.

JSON hỏng, sai ảnh hoặc kích thước ảnh đã thay đổi sẽ báo lỗi và không bị ghi đè tự động. Model cục bộ nằm mặc định ở `~/.paddlex/official_models/PP-OCRv6_medium_det`; tùy chọn `--model-dir` cho phép đổi đường dẫn. Cache riêng nằm trong output, hoặc theo `PADDLE_PDX_CACHE_HOME` nếu đã cấu hình. Không tự cài gói, tải model hay chuyển sang CPU khi GPU lỗi.

Môi trường đã kiểm tra: `paddleocr 3.7.0`, `paddlex 3.7.2`, `paddlepaddle-gpu 3.3.1` CUDA 12.6, RTX 4050 Laptop 6 GB. Model v6 là phiên bản mô hình, còn SDK PaddleOCR có số phiên bản 3.7.0. Tham khảo [Text Detection của PaddleOCR](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_detection.html).

## Kiểm tra

```bash
/home/nhminh/miniconda3/envs/OCR_Env/bin/python -m unittest discover -s tests -p test_region_tool.py -v
node --check source/region_tool/static/app.js
```

Module chuẩn bị vùng ảnh cho bước nhận dạng ký tự, hiệu đính và dóng hàng về sau; chưa nhận dạng nội dung chữ, tạo bản dịch hoặc xuất TSV/XLSX song song.
