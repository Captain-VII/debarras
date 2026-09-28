"""Formatage lisible des tailles, dates et durées."""
from __future__ import annotations

from datetime import datetime

_UNITS = ("o", "Ko", "Mo", "Go", "To", "Po")


def human_size(size: float) -> str:
    """1536 -> '1,5 Ko'."""
    for unit in _UNITS:
        if abs(size) < 1024 or unit == _UNITS[-1]:
            if unit == "o":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{size} o"


def human_date(ts: float | None) -> str:
    """Timestamp -> 'JJ/MM/AAAA HH:MM'."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def human_duration(seconds: float) -> str:
    """75.3 -> '1 min 15 s'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def human_count(n: int) -> str:
    """124190 -> '124 190' (espace insécable fine)."""
    return f"{n:,}".replace(",", " ")
