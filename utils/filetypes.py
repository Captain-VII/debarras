"""Catégories de fichiers par extension + couleurs associées (ordre fixe)."""
from __future__ import annotations

_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
               ".raw", ".cr2", ".nef", ".arw", ".dng", ".svg", ".ico", ".psd", ".xcf"),
    "Vidéos": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
               ".mpeg", ".ts", ".3gp"),
    "Audio": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".aiff"),
    "Documents": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
                  ".odp", ".txt", ".rtf", ".md", ".csv", ".epub", ".mobi"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".iso",
                 ".img", ".vhd", ".vhdx", ".cab"),
    "Code": (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
             ".go", ".rs", ".rb", ".php", ".html", ".css", ".json", ".xml", ".yml", ".yaml",
             ".toml", ".sql", ".sh", ".ps1", ".bat", ".ipynb", ".pyc", ".class", ".o", ".obj"),
    "Exécutables": (".exe", ".msi", ".dll", ".sys", ".appx", ".msix", ".jar", ".apk",
                    ".bin", ".so", ".dylib"),
}
OTHER = "Autres"
CATEGORIES: tuple[str, ...] = (*_EXTENSIONS, OTHER)

_BY_EXT = {ext: cat for cat, exts in _EXTENSIONS.items() for ext in exts}

# Palette catégorielle validée (daltonisme) : clair / sombre. "Autres" = gris neutre.
_COLORS_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#9a9993")
_COLORS_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#6b6a66")


def extensions(cat: str) -> tuple[str, ...]:
    """Extensions d'une catégorie ; pour « Autres », toutes les extensions connues (à exclure)."""
    if cat == OTHER:
        return tuple(_BY_EXT)
    return _EXTENSIONS.get(cat, ())


def category(ext: str) -> str:
    """'.JPG' -> 'Images'."""
    return _BY_EXT.get(ext.lower(), OTHER)


def category_colors(dark: bool) -> dict[str, str]:
    return dict(zip(CATEGORIES, _COLORS_DARK if dark else _COLORS_LIGHT))
