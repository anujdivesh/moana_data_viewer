"""Convert LAS/LAZ/COPC point clouds to 3D Tiles for Cesium."""

import os
import shutil
import tempfile
from pathlib import Path

import laspy
from py3dtiles.convert import convert
from pyproj import CRS

from .conversion import ConversionError, parse_crs

POINTCLOUD_SUFFIXES = (".las", ".laz")  # .copc.laz ends in .laz
# Cesium places 3D Tiles in Earth-centred coordinates.
ECEF = CRS.from_epsg(4978)


def to_3dtiles(src_path, out_dir, source_crs=None):
    """Write a 3D Tiles tileset for src_path to out_dir; return info stored on the upload."""
    try:
        with laspy.open(src_path) as reader:
            header = reader.header
            file_crs = header.parse_crs()
            dims = set(header.point_format.dimension_names)
            point_count = header.point_count
    except (laspy.LaspyException, OSError, ValueError) as exc:
        raise ConversionError(f"Could not read the point cloud: {exc}") from exc

    crs = parse_crs(source_crs) if source_crs else file_crs
    if crs is None:
        raise ConversionError(
            "The point cloud has no CRS. Enter the source CRS, e.g. EPSG:32760."
        )

    shutil.rmtree(out_dir, ignore_errors=True)
    Path(out_dir).parent.mkdir(parents=True, exist_ok=True)
    try:
        # Next to the output, as the uncompressed copy can be several GB.
        with tempfile.TemporaryDirectory(dir=Path(out_dir).parent) as tmp:
            flat = Path(tmp) / "points.las"
            _rewrite_sequentially(src_path, flat)
            convert(
                str(flat),
                outfolder=str(out_dir),
                crs_in=crs,
                crs_out=ECEF,
                force_crs_in=True,
                pyproj_always_xy=True,
                # More workers race on intermediate tile files on large clouds.
                jobs=min(4, os.cpu_count() or 2),
                overwrite=True,
            )
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise ConversionError(f"Could not convert the point cloud: {exc}") from exc

    epsg = crs.to_epsg()
    return {
        "points": point_count,
        "rgb": {"red", "green", "blue"} <= dims,
        "crs": f"EPSG:{epsg}" if epsg else crs.name,
    }


def _rewrite_sequentially(src_path, dst_path):
    """Copy a LAS/LAZ/COPC file to plain LAS by reading it start to finish.

    py3dtiles reads its input in parallel by seeking to point offsets, and seeking
    in COPC files returns zeroed points, so it must be given a file without chunked
    compression.
    """
    with laspy.open(src_path) as reader:
        source = reader.header
        header = laspy.LasHeader(point_format=source.point_format, version=source.version)
        header.scales, header.offsets = source.scales, source.offsets
        with laspy.open(dst_path, mode="w", header=header) as writer:
            for chunk in reader.chunk_iterator(2_000_000):
                writer.write_points(chunk)
