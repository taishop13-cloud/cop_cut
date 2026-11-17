"""CopCut – a lightweight video editor."""
import os
import sys

# Qt Multimedia: use the FFmpeg backend with hardware decoding when available.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "d3d11va,dxva2,cuda,qsv")

from PySide6.QtWidgets import QApplication  # noqa: E402

from copcut import theme  # noqa: E402
from copcut.main_window import MainWindow  # noqa: E402


def main():
    if sys.platform == "win32":
        # own taskbar identity/icon instead of the generic Python one
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CopCut.Editor")
    app = QApplication(sys.argv)
    app.setApplicationName("CopCut")
    app.setOrganizationName("CopCut")
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "copcut", "assets", "copcut.ico")))
    theme.apply(app)
    w = MainWindow()
    w.show()
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".copcut"):
            import json
            with open(arg, encoding="utf-8") as f:
                w._load_project(json.load(f), arg)
        elif os.path.isfile(arg):
            paths = w.import_files([arg])
            w.add_to_timeline(paths)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
