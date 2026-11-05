"""Files sent from a browser for a quick look, kept for settings.TEMP_UPLOAD_HOURS."""

import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from django.conf import settings

from .conversion import ConversionError
from .gridded import ZARR_MARKERS


def temp_dir():
    return Path(settings.CACHE_DIR) / "temp"


def save(uploaded):
    """Write an uploaded file to a folder of its own; return the path to open as a layer.

    A zip holding a Zarr store is extracted, as Zarr is read from a folder.
    """
    folder = temp_dir() / uuid.uuid4().hex
    folder.mkdir(parents=True)
    path = folder / PurePosixPath(uploaded.name).name
    try:
        with open(path, "wb") as dst:
            for chunk in uploaded.chunks():
                dst.write(chunk)
        if path.suffix.lower() == ".zip" and _zarr_root(path) is not None:
            store = _extract_zarr(path, folder / path.stem.removesuffix(".zarr"))
            path.unlink()
            return store
        return path
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def remove(path):
    """Delete the folder a temporary file was saved in; paths elsewhere are left alone."""
    path = Path(path)
    root = temp_dir()
    if path.is_relative_to(root) and path != root:
        shutil.rmtree(root / path.relative_to(root).parts[0], ignore_errors=True)


def _zarr_root(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        roots = [
            str(PurePosixPath(name).parent)
            for name in archive.namelist()
            if PurePosixPath(name).name in ZARR_MARKERS and "__MACOSX" not in name
        ]
    return min(roots, key=lambda r: (len(PurePosixPath(r).parts), r)) if roots else None


def _extract_zarr(zip_path, dest):
    root = _zarr_root(zip_path)
    prefix = "" if root == "." else root + "/"
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and m.filename.startswith(prefix)]
        if sum(m.file_size for m in members) > settings.MAX_UPLOAD_SIZE * 4:
            raise ConversionError("The Zarr store is too large once extracted.")
        for member in members:
            relative = PurePosixPath(member.filename[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts or "__MACOSX" in relative.parts:
                raise ConversionError(f"Unsafe path in zip: {member.filename}")
            target = dest.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest
