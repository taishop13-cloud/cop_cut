# CopCut

Trình biên tập video gọn nhẹ cho Windows, tập trung vào các thao tác cơ bản. Kéo timeline vẫn mượt với video dài 7–8 giờ.

## Tính năng

- **Cắt / ghép video**:
  - Rãnh chính tự dồn clip lại khi xóa hoặc cắt bớt.
  - Tách clip (Ctrl+B), xóa phần trái/phải đầu phát (Q / W).
  - Kéo mép clip để cắt bớt, kéo thân clip để đổi thứ tự.
  - Kéo thả tệp từ Explorer vào app.
- **Âm lượng** theo dB như CapCut (-∞ đến +20 dB, 0 dB = gốc) cho từng clip, có nút tắt tiếng. Nhạc nền đặt trên rãnh âm thanh riêng.
- **Tốc độ** 0.1x – 10x. Âm thanh giữ nguyên cao độ khi xuất.
- **Chữ / phụ đề**: chọn phông, cỡ, màu chữ, đậm/nghiêng, viền chữ (màu, độ dày) và hộp nền, độ trong suốt (0–100 %).
- **Ảnh / logo** (PNG, JPG, WEBP, BMP; giữ nền trong suốt của PNG): nút “🖼 Logo” (Ctrl+L) hoặc kéo tệp ảnh xuống timeline. Logo tự đặt trong **khung tròn** (ảnh không vuông được cắt lấy phần giữa, bỏ phần thừa; bỏ chọn “Khung tròn” trong Thuộc tính để giữ hình gốc). Logo nằm trên rãnh “Mờ / Logo”, kéo trên khung xem trước để di chuyển, kéo góc để đổi cỡ (giữ tỉ lệ), chỉnh độ trong suốt, đặt nhanh vào 4 góc; kéo hai mép trên timeline để đổi thời gian hiện.
- **Nhiều rãnh (multi-track)** cho chữ/phụ đề và âm thanh, rãnh tự thêm khi cần:
  - Kéo clip lên vùng trống trên cùng (chữ) hoặc xuống vùng trống dưới cùng (âm thanh) để tạo rãnh mới.
  - Thả clip hoặc tệp âm thanh đè lên clip khác: clip tự sang rãnh trống kế tiếp, hết rãnh thì tạo rãnh mới.
  - Rãnh trống tự xóa. Xem trước phát cùng lúc mọi rãnh âm thanh.
- **Chuyển phụ đề thành giọng nói** (Ctrl+Shift+T, nút “🔊 Đọc phụ đề” hoặc chuột phải vào phụ đề):
  - Chọn câu bất kỳ: Ctrl+nhấp / Shift+nhấp trên rãnh chữ, hoặc tích chọn trong hộp thoại (có tìm kiếm, lọc theo rãnh, “từ đầu phát trở đi”).
  - Chỉ giọng tiếng Việt:
    - **Ngọc Huyền, Ngọc Huyền Mới, Thiện Tâm**: chạy ngoại tuyến trên máy qua `piper-tts`, dùng mô hình của app H2DevTool TTS đã cài (`C:\Program Files\H2DevTool TTS\resources\models`). Mô hình không đi kèm CopCut.
    - **Hoài My, Nam Minh**: qua `edge-tts`, cần Internet khi tạo.
  - Mỗi câu thành một clip âm thanh đặt đúng thời điểm của câu đó, trên rãnh âm thanh trống (tự tạo rãnh mới bên dưới nếu cần). Tùy chọn tự đọc nhanh hơn cho vừa câu phụ đề.
  - Tệp giọng nói lưu ở `%LOCALAPPDATA%\CopCut	ts`; tạo lại cùng câu sẽ không tốn thời gian.
  - Kéo chữ trực tiếp trên khung xem trước để đổi vị trí.
  - Áp một kiểu chữ cho cả rãnh chữ đang chọn (rãnh tên kênh / thông báo giữ nguyên); chuột phải có thêm tùy chọn áp cho mọi rãnh.
  - Nhập/xuất phụ đề SRT.
- **Xuất MP4**:
  - Chọn độ phân giải, fps và chất lượng.
  - Tự dùng GPU nếu có (NVIDIA NVENC, Intel QuickSync, AMD AMF).
  - **Xuất nhanh không mã hóa lại** khi chỉ cắt/ghép.
- Hoàn tác/làm lại, lưu/mở dự án (`.copcut`).

## Vì sao nhẹ với video dài

- Timeline tự vẽ và chỉ vẽ phần đang hiện trên màn hình.
- Ảnh thu nhỏ được lấy từng khung theo nhu cầu, rồi lưu vào `%LOCALAPPDATA%\CopCut\cache`.
- Dạng sóng âm thanh chỉ giải mã một lần thành bảng 100 điểm/giây.
- Xem trước dùng trình phát của Qt (FFmpeg + giải mã phần cứng) đọc thẳng tệp gốc, không tạo bản proxy.
- Khi xuất, mỗi clip được tua thẳng tới điểm cắt (`-ss`), nên không phải giải mã lại từ đầu tệp 8 giờ. Dự án nhiều clip được xuất theo từng phần rồi ghép lại.

## Chạy

Nháy đúp `CopCut.bat`. Lần đầu, nó sẽ tạo `.venv`, cài thư viện và tải FFmpeg.

Hoặc chạy thủ công:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python setup_ffmpeg.py
.venv\Scripts\python run.py
```

Yêu cầu: Windows 10/11 và Python 3.10 trở lên.

Tạo icon CopCut trên Desktop và Start Menu (chạy sau khi đã cài đặt lần đầu):

```
powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
```

## Phím tắt

| Phím | Chức năng |
|---|---|
| Space | Phát / tạm dừng |
| ← / → | Lùi/tới 1 khung hình (Shift: 1 giây, Ctrl: 10 giây) |
| ↑ / ↓ | Nhảy tới điểm cắt trước/sau |
| Ctrl+B | Tách clip |
| Q / W | Xóa phần bên trái / phải đầu phát |
| Delete | Xóa clip đang chọn |
| Ctrl+T | Thêm chữ |
| Ctrl+Shift+T | Chuyển phụ đề thành giọng nói |
| Ctrl/Shift + nhấp | Chọn nhiều đoạn (phụ đề, âm thanh, video) để xóa / chỉnh âm lượng / tốc độ cùng lúc. Chuột phải → “Chọn mọi đoạn trên rãnh này” |
| Ctrl+Z / Ctrl+Y | Hoàn tác / làm lại |
| Ctrl + lăn chuột, + / − | Phóng to / thu nhỏ timeline |
| Shift+Z | Xem toàn bộ timeline |
| Ctrl+E | Xuất video |

## Cấu trúc mã

| Tệp | Nội dung |
|---|---|
| `copcut/model.py` | Dự án, clip, chữ |
| `copcut/timeline.py` | Timeline tự vẽ |
| `copcut/preview.py` | Khung xem trước và bộ phát |
| `copcut/cache.py` | Ảnh thu nhỏ và dạng sóng |
| `copcut/export.py` | Dựng lệnh FFmpeg |
| `copcut/subtitles.py` | SRT / ASS |
| `copcut/tts.py`, `copcut/tts_dialog.py` | Chuyển phụ đề thành giọng nói |
| `copcut/inspector.py` | Bảng thuộc tính |
| `copcut/media_bin.py` | Kho media |
| `copcut/main_window.py` | Cửa sổ chính |
