"""Render XYZ map tiles from GeoTIFFs, read in place (COGs are fastest, any GeoTIFF works)."""

import math
import warnings

import numpy
import rasterio
import rasterio.warp
from rasterio.errors import RasterioError
from rio_tiler.colormap import cmap
from rio_tiler.constants import WGS84_CRS
from rio_tiler.errors import NoOverviewWarning, RioTilerError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.utils import render

from .conversion import ConversionError, parse_crs

RASTER_SUFFIXES = (".tif", ".tiff")
# Plain GeoTIFFs are read as they are; slower than COGs at low zoom, but no need to say so per tile.
warnings.filterwarnings("ignore", category=NoOverviewWarning)
DEFAULT_COLORMAP = "viridis"

# GDAL settings for reading remote COGs efficiently over HTTP range requests.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "VSI_CACHE": "TRUE",
}

EMPTY_TILE = render(
    numpy.zeros((1, 256, 256), dtype="uint8"),
    mask=numpy.zeros((256, 256), dtype="uint8"),
    img_format="PNG",
)


def with_crs(path, source_crs):
    """A GDAL source for a local raster that assigns source_crs, without rewriting the file."""
    epsg = parse_crs(source_crs).to_epsg()
    if epsg is None:
        raise ConversionError("Enter the raster's source CRS as an EPSG code, e.g. EPSG:32760.")
    return f"vrt://{path}?a_srs=EPSG:{epsg}"


def describe(src):
    """Return the display settings stored on a layer for a GeoTIFF path or URL."""
    try:
        with rasterio.Env(**GDAL_ENV), Reader(src) as reader:
            dataset = reader.dataset
            if dataset.crs is None:
                raise ConversionError(
                    "The GeoTIFF has no CRS. Enter the source CRS, e.g. EPSG:32760."
                )
            west, south, east, north = reader.get_geographic_bounds(WGS84_CRS)
            rgb = dataset.count >= 3 and dataset.dtypes[0] == "uint8"
            info = {
                # west > east when the raster crosses the antimeridian.
                "bounds": [west, south, east, north],
                "maxzoom": _native_zoom(west, east, dataset.width),
                "band_count": dataset.count,
                "dtype": dataset.dtypes[0],
                "crs": _crs_label(dataset.crs),
                "rgb": rgb,
            }
            if not rgb:
                stats = reader.statistics(indexes=1, percentiles=[2, 98])["b1"]
                low, high = stats.percentile_2, stats.percentile_98
                if not (math.isfinite(low) and math.isfinite(high)):
                    low, high = stats.min, stats.max
                if low == high:
                    high = low + 1
                info["rescale"] = [low, high]
                info["colormap"] = DEFAULT_COLORMAP
            return info
    except (RasterioError, RioTilerError) as exc:
        raise ConversionError(f"Could not read the GeoTIFF: {exc}") from exc


def render_tile(src, info, z, x, y, colormap=None, rescale=None):
    """Render one WebMercatorQuad tile as PNG bytes."""
    indexes = (1, 2, 3) if info["rgb"] else (1,)
    with rasterio.Env(**GDAL_ENV), Reader(src) as reader:
        try:
            image = reader.tile(x, y, z, indexes=indexes)
        except TileOutsideBounds:
            return EMPTY_TILE

    if info["rgb"]:
        return image.render(img_format="PNG")
    return colorize(image, rescale or info["rescale"], colormap or info["colormap"])


def value_at(src, lon, lat):
    """Band values at a lon/lat point, {"Band 1": 12.3, ...}; None outside or on NoData."""
    with rasterio.Env(**GDAL_ENV), rasterio.open(src) as dataset:
        xs, ys = rasterio.warp.transform(WGS84_CRS, dataset.crs, [lon], [lat])
        row, col = dataset.index(xs[0], ys[0])
        if not (0 <= row < dataset.height and 0 <= col < dataset.width):
            return None
        pixel = dataset.read(window=((row, row + 1), (col, col + 1)), masked=True)[:, 0, 0]
    if numpy.ma.getmaskarray(pixel).all():
        return None
    return {
        f"Band {i + 1}": None if numpy.ma.is_masked(v) else float(v)
        for i, v in enumerate(pixel)
    }


def colorize(image, rescale, colormap):
    """Stretch a single-band ImageData to rescale=(min, max) and render it as PNG."""
    low, high = rescale
    image = image.rescale(in_range=[(low, high)])
    return image.render(img_format="PNG", colormap=cmap.get(colormap))


def _native_zoom(west, east, width):
    """WebMercatorQuad zoom whose 256px tiles match the raster's longitude resolution.

    Computed here because rio-tiler's maxzoom is wrong for rasters crossing the antimeridian.
    """
    span = east - west if east > west else east + 360 - west
    return zoom_for_resolution(span / width)


def zoom_for_resolution(degrees_per_pixel):
    """Smallest WebMercatorQuad zoom whose 256px tiles are at least as fine as the data."""
    if degrees_per_pixel <= 0:
        return 0
    return max(0, min(24, math.ceil(math.log2(360 / (256 * degrees_per_pixel)))))


def colormap_names():
    return cmap.list()


def _crs_label(crs):
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.to_string()
