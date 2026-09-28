"""Génère le logo de Débarras : assets/icon.ico (multi-tailles), assets/icon.png, assets/logo.png.

Un mini treemap (la carte du disque) dont une tuile se soulève et quitte sa case :
l'emplacement vide, c'est l'espace récupéré. En 16-24 px, version simplifiée.
Lancer : python assets/make_icon.py
"""
import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

BG_TOP, BG_BOTTOM = QColor("#2f3480"), QColor("#191c47")
ORANGE, BLUE, AQUA, YELLOW, VIOLET = (QColor(c) for c in ("#f0703c", "#4a93f0", "#22b884", "#f2b01e", "#a497f5"))
SLOT = QColor(255, 255, 255, 150)


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 100, size / 100)  # dessin sur une grille 100 × 100
    small = size <= 24

    # Fond : carré arrondi, dégradé nuit.
    bg = QLinearGradient(0, 0, 0, 100)
    bg.setColorAt(0, BG_TOP)
    bg.setColorAt(1, BG_BOTTOM)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(QRectF(2, 2, 96, 96), 22, 22)

    radius = 5 if small else 4

    def tile(r: QRectF, color: QColor) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(r, radius, radius)

    # Treemap : grande tuile, tuiles moyennes et petites (la carte du disque).
    tile(QRectF(14, 14, 38, 72), ORANGE)
    if small:
        tile(QRectF(56, 52, 30, 34), AQUA)
    else:
        tile(QRectF(56, 52, 15, 34), AQUA)
        tile(QRectF(74, 52, 12, 18), YELLOW)
        tile(QRectF(74, 73, 12, 13), VIOLET)

    # Case libérée (en haut à droite).
    slot = QRectF(56, 14, 30, 34)
    pen = QPen(SLOT, 5 if small else 2.6)
    if not small:
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([2.2, 1.6])
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = 2.5 if small else 1.3
    p.drawRoundedRect(slot.adjusted(inset, inset, -inset, -inset), radius, radius)

    if not small:
        # La tuile qui s'en va : soulevée, inclinée, avec une ombre douce.
        p.save()
        p.translate(QPointF(83, 18))
        p.rotate(18)
        piece = QRectF(-11.5, -12.5, 23, 25)
        p.setBrush(QColor(0, 0, 0, 70))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(piece.translated(2.2, 3), radius, radius)
        tile(piece, BLUE)
        p.restore()
    p.end()
    return img


def _png(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(path: str, sizes=ICO_SIZES) -> None:
    """ICO multi-tailles à images PNG (format accepté par Windows depuis Vista)."""
    images = [_png(draw(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, png in zip(sizes, images):
        dim = 0 if s >= 256 else s  # 0 signifie 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def main() -> None:
    app = QGuiApplication(sys.argv)  # noqa: F841 - requis pour QPainter
    write_ico(os.path.join(HERE, "icon.ico"))
    draw(256).save(os.path.join(HERE, "icon.png"))
    draw(1024).save(os.path.join(HERE, "logo.png"))
    # Planche d'aperçu : toutes les tailles côte à côte, sur fond clair et sombre.
    sheet = QImage(900, 330, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(sheet)
    for row, bgc in enumerate(("#f3f2ee", "#202020")):
        p.fillRect(0, row * 165, 900, 165, QColor(bgc))
        x = 10
        for s in (16, 24, 32, 48, 64, 128):
            p.drawImage(x, row * 165 + (165 - s) // 2, draw(s))
            x += s + 24
    p.end()
    sheet.save(os.path.join(HERE, "..", "build", "icon_preview.png"))
    print("icône générée")


if __name__ == "__main__":
    main()
