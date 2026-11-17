from PySide6.QtGui import QColor, QPalette

STYLE = """
QWidget { background: #17191e; color: #d6dae3; font-size: 9pt; }
QMainWindow::separator, QSplitter::handle { background: #0f1013; }
QSplitter::handle:horizontal { width: 3px; } QSplitter::handle:vertical { height: 3px; }
QMenuBar { background: #121317; } QMenuBar::item:selected { background: #2a2e38; }
QMenu { background: #1f2229; border: 1px solid #2c303a; }
QMenu::item:selected { background: #4f8cff; color: white; }
QLabel#panelTitle { background: #1d2026; padding: 7px 10px; font-weight: 600; color: #e9ecf3;
                    border-bottom: 1px solid #262a33; }
QLabel#hint { color: #7d8494; font-size: 8pt; }
QLabel#clipName { font-weight: 600; font-size: 10pt; }
QLabel#timecode { font-family: Consolas, monospace; font-size: 10pt; color: #4f8cff; }
QLabel#timecodeDim { font-family: Consolas, monospace; font-size: 9pt; color: #7d8494; }
QToolButton { padding: 4px 7px; border-radius: 4px; }
QToolButton:hover { background: #2a2e38; }
QToolButton:checked { background: #2d3d5e; color: #ffffff; }
QToolButton#playBtn { font-size: 14pt; padding: 2px 14px; }
QPushButton { background: #262a33; border: 1px solid #323743; border-radius: 5px; padding: 5px 12px; }
QPushButton:hover { background: #2f3440; }
QPushButton:disabled { color: #5c6270; }
QPushButton#accentBtn { background: #4f8cff; border: none; color: white; font-weight: 600; }
QPushButton#accentBtn:hover { background: #6a9dff; }
QListWidget { background: #14161a; border: none; }
QListWidget::item { color: #b9bfcc; border-radius: 5px; padding: 3px; }
QListWidget::item:selected { background: #2d3d5e; color: white; }
QGroupBox { border: 1px solid #2a2e38; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa1b0; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox {
    background: #101215; border: 1px solid #2c303a; border-radius: 4px; padding: 3px 5px; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #4f8cff; }
QComboBox QAbstractItemView { background: #1f2229; selection-background-color: #4f8cff; }
QSlider::groove:horizontal { height: 4px; background: #2c303a; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4f8cff; border-radius: 2px; }
QSlider::handle:horizontal { background: #e9ecf3; width: 12px; margin: -5px 0; border-radius: 6px; }
QScrollBar:horizontal { height: 12px; background: #121317; }
QScrollBar::handle:horizontal { background: #353a46; min-width: 30px; border-radius: 5px; margin: 2px; }
QScrollBar:vertical { width: 10px; background: #121317; }
QScrollBar::handle:vertical { background: #353a46; min-height: 30px; border-radius: 4px; margin: 2px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QProgressBar { background: #101215; border: 1px solid #2c303a; border-radius: 4px; height: 16px;
               text-align: center; }
QProgressBar::chunk { background: #4f8cff; border-radius: 3px; }
QStatusBar { background: #121317; color: #9aa1b0; }
QCheckBox::indicator { width: 14px; height: 14px; }
"""


def apply(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, c in ((QPalette.Window, "#17191e"), (QPalette.WindowText, "#d6dae3"),
                    (QPalette.Base, "#101215"), (QPalette.AlternateBase, "#1b1e24"),
                    (QPalette.Text, "#d6dae3"), (QPalette.Button, "#262a33"),
                    (QPalette.ButtonText, "#d6dae3"), (QPalette.Highlight, "#4f8cff"),
                    (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#1f2229"),
                    (QPalette.ToolTipText, "#d6dae3")):
        pal.setColor(role, QColor(c))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
