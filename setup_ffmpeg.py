"""Download a Windows FFmpeg build (gyan.dev 'essentials') into ./ffmpeg/bin.

Verifies the published SHA-256 checksum before extracting ffmpeg.exe and ffprobe.exe.
"""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DEST = Path(__file__).resolve().parent / "ffmpeg" / "bin"


def main():
    if sys.platform != "win32":
        print("Trên Linux/macOS hãy cài ffmpeg bằng trình quản lý gói (apt/brew).")
        return
    print("Đang tải FFmpeg …")
    data = urllib.request.urlopen(URL, timeout=120).read()
    expected = urllib.request.urlopen(URL + ".sha256", timeout=60).read().decode().split()[0].lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        sys.exit(f"Sai checksum! {actual} != {expected}")
    DEST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1]
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                (DEST / base).write_bytes(z.read(name))
                print("  ->", DEST / base)
    print("Xong.")


if __name__ == "__main__":
    main()
