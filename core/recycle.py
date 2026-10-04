"""Capacité de la corbeille Windows, pour ne jamais provoquer de suppression définitive.

Envoyer à la corbeille plus que sa place libre fait effacer définitivement par Windows les
éléments les plus anciens ; un élément plus gros que sa capacité est refusé ; et sur un
lecteur réglé « ne pas utiliser la corbeille », tout est effacé directement.
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

_BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"


@dataclass(frozen=True)
class BinInfo:
    drive: str
    capacity: int | None   # octets ; None si inconnue
    used: int
    disabled: bool         # « Supprimer immédiatement sans utiliser la corbeille »

    @property
    def free(self) -> int | None:
        return None if self.capacity is None else max(self.capacity - self.used, 0)


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def _used(drive: str) -> int:
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(info)) == 0:
            return int(info.i64Size)
    except (AttributeError, OSError):
        pass
    return 0


def _volume_guid(drive: str) -> str | None:
    """'C:\\' -> '{d8a4cfe7-…}' (identifiant de volume utilisé par les réglages de la corbeille)."""
    try:
        buf = ctypes.create_unicode_buffer(64)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, buf, len(buf)):
            name = buf.value  # \\?\Volume{guid}\
            start, end = name.find("{"), name.find("}")
            if start >= 0 and end > start:
                return name[start:end + 1].lower()
    except (AttributeError, OSError):
        pass
    return None


def bin_info(drive: str) -> BinInfo:
    drive = os.path.splitdrive(drive)[0].upper() + "\\"
    capacity, disabled = None, False
    guid = _volume_guid(drive)
    if guid:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BITBUCKET) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    name = winreg.EnumKey(root, i)
                    if name.lower() != guid:
                        continue
                    with winreg.OpenKey(root, name) as k:
                        try:
                            capacity = int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
                        except OSError:
                            pass
                        try:
                            disabled = bool(winreg.QueryValueEx(k, "NukeOnDelete")[0])
                        except OSError:
                            pass
        except (ImportError, OSError):
            pass
    return BinInfo(drive, capacity, _used(drive), disabled)


def capacity_problems(paths: list[str], sizes: dict[str, int],
                      info=bin_info) -> list[str]:
    """Raisons de ne pas envoyer `paths` à la corbeille (vide si tout tient)."""
    by_drive: dict[str, list[str]] = {}
    for p in paths:
        by_drive.setdefault(os.path.splitdrive(os.path.abspath(p))[0].upper(), []).append(p)
    norm_sizes = {os.path.normcase(os.path.abspath(k)): v for k, v in sizes.items()}
    out: list[str] = []
    for drive, items in by_drive.items():
        if not drive or drive.startswith("\\\\"):
            continue  # partage réseau : pas de corbeille, send2trash refusera
        b = info(drive + "\\")
        total = sum(norm_sizes.get(os.path.normcase(os.path.abspath(p)), 0) for p in items)
        biggest = max((norm_sizes.get(os.path.normcase(os.path.abspath(p)), 0) for p in items), default=0)
        if b.disabled:
            out.append(f"{drive} : la corbeille est désactivée sur ce lecteur, les éléments seraient "
                       "effacés définitivement. Réactivez-la (Propriétés de la Corbeille) ou déplacez-les.")
        elif b.capacity is not None and biggest > b.capacity:
            out.append(f"{drive} : un élément ({_h(biggest)}) dépasse la capacité de la corbeille "
                       f"({_h(b.capacity)}). Sélectionnez son contenu plutôt que le dossier entier.")
        elif b.free is not None and total > b.free:
            out.append(f"{drive} : la sélection ({_h(total)}) dépasse la place libre dans la corbeille "
                       f"({_h(b.free)} sur {_h(b.capacity or 0)}). Windows effacerait définitivement les "
                       "éléments les plus anciens de la corbeille : videz-la d'abord.")
    return out


def _h(n: int) -> str:
    from utils.format import human_size
    return human_size(n)
