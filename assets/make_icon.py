"""Génère assets/icon.ico (mini treemap aux couleurs de l'application). À lancer une fois."""
import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath

HERE = os.path.dirname(os.path.abspath(__file__))

# (x, y, w, h) sur une grille de 16, couleur de catégorie
BLOCKS = [
    (0, 0, 9, 16, "#d95926"),   # Vidéos
    (9, 0, 7, 7, "#3987e5"),    # Images
    (9, 7, 4, 9, "#199e70"),    # Audio
    (13, 7, 3, 5, "#c98500"),   # Documents
    (13, 12, 3, 4, "#9085e9"),  # Exécutables
]


def main() -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841 - requis pour QPainter
    size = 256
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(8, 8, size - 16, size - 16), 36, 36)
    p.setClipPath(clip)
    p.fillRect(img.rect(), QColor("#1a1a19"))
    unit, gap, origin = (size - 16) / 16, 6, 8
    for x, y, w, h, color in BLOCKS:
        r = QRectF(origin + x * unit + gap / 2, origin + y * unit + gap / 2, w * unit - gap, h * unit - gap)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(r, 10, 10)
    p.end()
    out = os.path.join(HERE, "icon.ico")
    if not img.save(out, "ICO"):
        raise SystemExit("Échec de l'écriture de l'icône")
    img.save(os.path.join(HERE, "icon.png"), "PNG")
    print(out)


if __name__ == "__main__":
    main()
