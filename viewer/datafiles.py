"""Browse the data folders (settings.DATA_ROOTS) and tell what each file is."""

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from django.conf import settings

from .conversion import ConversionError
from .gridded import NETCDF_SUFFIXES, ZARR_MARKERS

SUFFIX_KINDS = {
    ".geojson": "vector",
    ".json": "vector",
    ".shp": "vector",
    ".zip": "vector",
    ".tif": "raster",
    ".tiff": "raster",
    ".las": "pointcloud",
    ".laz": "pointcloud",
    ".pmtiles": "pmtiles",
    **{suffix: "netcdf" for suffix in NETCDF_SUFFIXES},
}
MAX_ENTRIES = 1000


def kind_for(path):
    """The layer kind a file or folder opens as, or None if the viewer can't show it."""
    path = Path(path)
    if path.is_dir():
        return "zarr" if any((path / marker).exists() for marker in ZARR_MARKERS) else None
    return SUFFIX_KINDS.get(path.suffix.lower())


def normalize(value):
    """A typed or pasted path as this OS spells it: Windows (C:\\data, \\\\server\\share),
    Linux/macOS (/data), file:// URLs, with or without surrounding quotes."""
    value = value.strip().strip('"\'').strip()
    if value.lower().startswith("file:"):
        parsed = urlparse(value)
        value = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc != "localhost":
            value = f"//{parsed.netloc}{value}"  # file://server/share/...
        if os.name != "nt" and re.match(r"^/[A-Za-z]:", value):
            value = value[1:]  # file:///C:/... on a non-Windows server
    windows_path = re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")
    if windows_path and os.name != "nt":
        raise ConversionError(
            f"{value} is a Windows path, but the viewer's server runs on "
            f"{'macOS' if os.uname().sysname == 'Darwin' else 'Linux'}. Enter a path on that machine."
        )
    return value


def resolve(value):
    """An existing path inside one of the data folders; anything else is refused."""
    if not settings.DATA_ROOTS:
        raise ConversionError("No data folders are set. Add DATA_ROOTS to .env.")
    value = normalize(value)
    path = Path(value).expanduser().resolve()
    if not any(path.is_relative_to(root) for root in settings.DATA_ROOTS):
        raise ConversionError(f"{value} is outside the data folders (DATA_ROOTS in .env).")
    if not path.exists():
        raise ConversionError(f"{value} does not exist.")
    return path


def listing(value=None):
    """Folders and viewable files in a data folder; the data folders themselves if no path."""
    if not value:
        entries = [_entry(root, name=str(root)) for root in settings.DATA_ROOTS if root.is_dir()]
        return {"path": None, "parent": None, "entries": entries, "truncated": False}

    path = resolve(value)
    if not path.is_dir():
        raise ConversionError(f"{value} is not a folder.")
    try:
        entries = [_entry(child) for child in path.iterdir() if not child.name.startswith(".")]
    except PermissionError:
        raise ConversionError(f"No permission to read {path}.") from None
    entries = [e for e in entries if e["dir"] or e["kind"]]
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = None if path in settings.DATA_ROOTS else str(path.parent)
    return {
        "path": str(path),
        "parent": parent,
        "entries": entries[:MAX_ENTRIES],
        "truncated": len(entries) > MAX_ENTRIES,
    }


def _entry(path, name=None):
    kind = kind_for(path)
    return {
        "name": name or path.name,
        "path": str(path),
        # Zarr stores are folders, but open as a layer rather than being browsed into.
        "dir": path.is_dir() and kind is None,
        "kind": kind,
    }
