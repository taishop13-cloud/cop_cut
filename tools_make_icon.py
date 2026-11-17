"""Render the CopCut app icon to copcut/assets/copcut.ico and .png."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QLinearGradient, QPainterPath, QPen

app = QGuiApplication(sys.argv)
S = 256
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
g = QLinearGradient(0, 0, S, S)
g.setColorAt(0, QColor("#5b9bff"))
g.setColorAt(1, QColor("#2a4fd6"))
p.setBrush(g); p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, S - 16, S - 16), 56, 56)
# film strip bar
p.setBrush(QColor(255, 255, 255, 60))
p.drawRoundedRect(QRectF(40, 176, 176, 34), 8, 8)
p.setBrush(QColor("#2a4fd6"))
for i in range(6):
    p.drawRect(QRectF(50 + i * 28, 186, 14, 14))
# play triangle
tri = QPainterPath()
tri.moveTo(98, 56); tri.lineTo(98, 156); tri.lineTo(182, 106); tri.closeSubpath()
p.setBrush(QColor("#ffffff")); p.drawPath(tri)
# cut line
p.setPen(QPen(QColor("#ffcc33"), 10, Qt.SolidLine, Qt.RoundCap))
p.drawLine(QPointF(66, 40), QPointF(66, 222))
p.end()
out = os.path.join(os.path.dirname(__file__), "copcut", "assets")
img.save(os.path.join(out, "copcut.png"))
ok = img.save(os.path.join(out, "copcut.ico"))
print("ico saved:", ok)
