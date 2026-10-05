"""Disques du PC (lettres, nom, taille, place libre) et droits administrateur."""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass

# GetDriveTypeW
_REMOVABLE, _FIXED, _REMOTE, _CDROM, _RAMDISK = 2, 3, 4, 5, 6


@dataclass(frozen=True)
class Drive:
    root: str          # 'C:\\'
    label: str         # nom du volume (« Windows », « Jeux »…)
    kind: str          # « Disque local », « Clé USB / amovible », « Lecteur réseau »
    total: int
    free: int

    @property
    def used(self) -> int:
        return max(self.total - self.free, 0)

    @property
    def title(self) -> str:
        letter = self.root.rstrip("\\")
        return f"{self.label} ({letter})" if self.label else f"{self.kind} ({letter})"


def _space(root: str) -> tuple[int, int]:
    free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)):
        raise OSError(root)
    return total.value, free.value


def _label(root: str) -> str:
    buf = ctypes.create_unicode_buffer(261)
    if ctypes.windll.kernel32.GetVolumeInformationW(root, buf, len(buf), None, None, None, None, 0):
        return buf.value
    return ""


def list_drives() -> list[Drive]:
    """Disques locaux, amovibles et réseau prêts (lecteurs de CD et disques vides ignorés)."""
    if sys.platform != "win32":
        return []
    out: list[Drive] = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(root)
        if kind not in (_FIXED, _REMOVABLE, _REMOTE, _RAMDISK):
            continue
        try:
            total, free = _space(root)
        except OSError:
            continue  # lecteur non prêt (carte SD absente…)
        if total <= 0:
            continue
        name = {_REMOVABLE: "Clé USB / amovible", _REMOTE: "Lecteur réseau"}.get(kind, "Disque local")
        out.append(Drive(root, _label(root), name, total, free))
    return out


def drive_of(path: str) -> Drive | None:
    """Disque dont `path` est la racine (None pour un simple dossier)."""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    if rest.strip("\\/"):
        return None
    root = drive.upper() + "\\"
    return next((d for d in list_drives() if d.root == root), None)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(args: list[str]) -> bool:
    """Relance Débarras avec les droits administrateur (fenêtre UAC). True si accepté."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, args
    else:  # développement : python main.py
        exe = sys.executable
        params = [os.path.abspath(sys.argv[0]), *args]
    line = " ".join(f'"{a}"' for a in params)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, line, None, 1)
    return rc > 32
