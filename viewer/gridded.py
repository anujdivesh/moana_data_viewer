"""Read Zarr stores and NetCDF files with xarray and render XYZ map tiles from regular lat/lon grids."""

import functools
import os
from pathlib import Path

import morecantile
import numpy
import pandas
import xarray
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import Resampling, reproject
from rio_tiler.models import ImageData

from .conversion import ConversionError
from .raster import DEFAULT_COLORMAP, EMPTY_TILE, colorize, zoom_for_resolution

# Files that mark a directory as a Zarr store (v2 and v3).
ZARR_MARKERS = {".zgroup", ".zarray", ".zmetadata", "zarr.json"}
NETCDF_SUFFIXES = (".nc", ".nc4", ".netcdf", ".cdf")

TILE_SIZE = 256
WEB_MERCATOR = morecantile.tms.get("WebMercatorQuad")


def format_for(src):
    """ "netcdf" for a local NetCDF file, "zarr" for a local store folder."""
    return "netcdf" if Path(src).is_file() else "zarr"


@functools.lru_cache(maxsize=16)
def open_store(src, fmt):
    """A Zarr store or NetCDF file, local or remote, opened the same way."""
    # chunks=None reads lazily without dask: only the chunks a tile needs are loaded.
    try:
        if fmt == "netcdf":
            return xarray.open_dataset(_netcdf_source(src), chunks=None)
        return xarray.open_zarr(_zarr_source(src), chunks=None, consolidated=None)
    except Exception as exc:
        kind = "NetCDF file" if fmt == "netcdf" else "Zarr store"
        raise ConversionError(f"Could not open the {kind}: {exc}") from exc


def _netcdf_source(src):
    if not src.startswith(("http://", "https://", "s3://")):
        return src
    _set_netcdf_ca_bundle()
    from .remote import https_url

    url = https_url(src)
    # OPeNDAP servers are read as they are; plain web files in byte ranges.
    if "/dodsc/" in url.lower() or "/opendap/" in url.lower() or "#mode=" in url:
        return url
    return f"{url}#mode=bytes"


def _zarr_source(src):
    from .remote import https_url

    # Remote stores are read over https (fsspec), so s3:// becomes its public address.
    return https_url(src)


@functools.cache
def _set_netcdf_ca_bundle():
    # netCDF-C uses its own copy of the certifi bundle; networks that re-sign HTTPS
    # (e.g. Zscaler) need the bundle from SSL_CERT_FILE instead.
    bundle = os.environ.get("SSL_CERT_FILE")
    if bundle:
        import netCDF4

        netCDF4.rc_set("HTTP.SSL.CAINFO", bundle)


def describe(src, fmt=None):
    """Return the display settings stored on a layer for a Zarr store or NetCDF file."""
    fmt = fmt or format_for(src)
    ds = open_store(src, fmt)
    lat_name = _find_axis(ds, "lat")
    lon_name = _find_axis(ds, "lon")
    if not lat_name or not lon_name:
        raise ConversionError(
            "No latitude/longitude coordinates found. Only regular lat/lon grids are supported."
        )
    lats = ds[lat_name].values.astype("float64")
    lons = ds[lon_name].values.astype("float64")
    dlat = _regular_step(lats, lat_name)
    dlon = _regular_step(lons, lon_name)

    variables = {}
    with_data = []
    for name, da in ds.data_vars.items():
        if lat_name not in da.dims or lon_name not in da.dims:
            continue
        if not numpy.issubdtype(da.dtype, numpy.number):
            continue
        extra_dims = [d for d in da.dims if d not in (lat_name, lon_name)]
        dims = [_dim_info(ds, d) for d in extra_dims]
        default_slice = da.isel({d["name"]: d["default"] for d in dims})
        rescale = _default_rescale(default_slice, lat_name, lon_name)
        if rescale:
            with_data.append(name)
        variables[name] = {
            "long_name": str(da.attrs.get("long_name") or da.attrs.get("standard_name") or name),
            "units": str(da.attrs.get("units", "")),
            "dims": dims,
            "rescale": rescale or [0.0, 1.0],
        }
    if not variables:
        raise ConversionError(f"No numeric variables on the {lat_name}/{lon_name} grid.")

    lon360 = bool(lons.max() > 180)
    return {
        "format": fmt,
        "lat": lat_name,
        "lon": lon_name,
        "lon360": lon360,
        "bounds": _bounds(lons, lats, dlon, dlat, lon360),
        "maxzoom": zoom_for_resolution(dlon),
        "crs": "EPSG:4326",
        "rgb": False,
        "colormap": DEFAULT_COLORMAP,
        "variables": variables,
        # Open on a variable that has data in its default slice, so the map isn't blank.
        "default_variable": (with_data or list(variables))[0],
    }


def render_tile(path, info, z, x, y, variable, sel, colormap=None, rescale=None):
    """Render one WebMercatorQuad tile of variable at the dimension indexes in sel."""
    var_info = info["variables"][variable]
    lat_name, lon_name = info["lat"], info["lon"]
    ds = open_store(path, info.get("format") or format_for(path))
    da = ds[variable].isel(
        {d["name"]: sel.get(d["name"], d.get("default", 0)) for d in var_info["dims"]}
    )

    tile_bounds = WEB_MERCATOR.bounds(x, y, z)
    # On a 0–360 grid, a tile in the western hemisphere maps onto the 180–360 part by a
    # constant shift. Only the zoom-0 tile spans both hemispheres: draw both parts.
    if not info["lon360"]:
        offsets = [0]
    elif tile_bounds.right <= 0:
        offsets = [360]
    elif tile_bounds.left < 0:
        offsets = [0, 360]
    else:
        offsets = [0]
    lons = ds[lon_name].values.astype("float64")
    lats = ds[lat_name].values.astype("float64")
    tile = numpy.full((TILE_SIZE, TILE_SIZE), numpy.nan, dtype="float32")
    for offset in offsets:
        part = _draw_part(da, lat_name, lon_name, lats, lons, offset, tile_bounds, (x, y, z))
        if part is not None:
            tile = numpy.where(numpy.isnan(tile), part, tile)
    masked = numpy.ma.masked_invalid(tile)
    if masked.mask.all():
        return EMPTY_TILE
    return colorize(
        ImageData(masked[numpy.newaxis]),
        rescale or var_info["rescale"],
        colormap or info["colormap"],
    )


def value_at(path, info, lon, lat, variable, sel):
    """The variable's value in the grid cell holding lon/lat, or None outside the grid."""
    var_info = info["variables"][variable]
    lat_name, lon_name = info["lat"], info["lon"]
    ds = open_store(path, info.get("format") or format_for(path))
    lons = ds[lon_name].values.astype("float64")
    lats = ds[lat_name].values.astype("float64")
    if info["lon360"] and lon < 0:
        lon += 360
    i_lon = int(numpy.abs(lons - lon).argmin())
    i_lat = int(numpy.abs(lats - lat).argmin())
    # Nearest cell, but only if the point is actually inside it.
    if abs(lons[i_lon] - lon) > abs(lons[1] - lons[0]) or abs(lats[i_lat] - lat) > abs(lats[1] - lats[0]):
        return None
    da = ds[variable].isel(
        {d["name"]: sel.get(d["name"], d.get("default", 0)) for d in var_info["dims"]}
    )
    value = float(da.isel({lat_name: i_lat, lon_name: i_lon}).values)
    return None if not numpy.isfinite(value) else value


def _draw_part(da, lat_name, lon_name, lats, lons, offset, tile_bounds, xyz):
    """The tile's pixels from the grid shifted west by offset degrees; None if none overlap."""
    lon_slice = _index_slice(lons, tile_bounds.left + offset, tile_bounds.right + offset)
    lat_slice = _index_slice(lats, tile_bounds.bottom, tile_bounds.top)
    if lon_slice is None or lat_slice is None:
        return None

    sub = da.isel({lat_name: lat_slice, lon_name: lon_slice}).transpose(lat_name, lon_name)
    values = sub.values.astype("float32")
    sub_lats = sub[lat_name].values.astype("float64")
    sub_lons = sub[lon_name].values.astype("float64") - offset
    if sub_lats[0] < sub_lats[-1]:
        values, sub_lats = values[::-1], sub_lats[::-1]

    dlon = abs(sub_lons[1] - sub_lons[0]) if len(sub_lons) > 1 else abs(lons[1] - lons[0])
    dlat = abs(sub_lats[1] - sub_lats[0]) if len(sub_lats) > 1 else abs(lats[1] - lats[0])
    part = numpy.full((TILE_SIZE, TILE_SIZE), numpy.nan, dtype="float32")
    reproject(
        values,
        part,
        src_transform=from_origin(sub_lons[0] - dlon / 2, sub_lats[0] + dlat / 2, dlon, dlat),
        src_crs="EPSG:4326",
        src_nodata=numpy.nan,
        dst_transform=from_bounds(*WEB_MERCATOR.xy_bounds(*xyz), TILE_SIZE, TILE_SIZE),
        dst_crs="EPSG:3857",
        dst_nodata=numpy.nan,
        resampling=Resampling.nearest,
    )
    return part


def _find_axis(ds, axis):
    """Name of the 1D latitude or longitude coordinate, found by CF attributes or common names."""
    units = {"lat": "degrees_north", "lon": "degrees_east"}[axis]
    standard_name = {"lat": "latitude", "lon": "longitude"}[axis]
    names = {"lat": ("lat", "latitude"), "lon": ("lon", "longitude")}[axis]
    candidates = list(ds.coords) + list(ds.dims)
    for name in candidates:
        if name in ds.variables:
            attrs = ds[name].attrs
            if attrs.get("standard_name") == standard_name or attrs.get("units") == units:
                return name
    for name in candidates:
        if str(name).lower() in names:
            return name
    return None


def _regular_step(values, name):
    if values.ndim != 1:
        raise ConversionError(
            f"{name} is {values.ndim}D (a curvilinear grid); only regular lat/lon grids are supported."
        )
    if len(values) < 2:
        raise ConversionError(f"{name} needs at least two values.")
    # Judge positions, not steps: float32 coordinates (e.g. 0.01° grids) have steps that
    # wobble with rounding, while every cell still sits where a regular grid puts it.
    step = (values[-1] - values[0]) / (len(values) - 1)
    expected = values[0] + step * numpy.arange(len(values))
    if step == 0 or numpy.abs(values - expected).max() > 0.05 * abs(step):
        raise ConversionError(f"{name} is not evenly spaced; only regular grids are supported.")
    return abs(float(step))


def _bounds(lons, lats, dlon, dlat, lon360):
    west, east = lons.min() - dlon / 2, lons.max() + dlon / 2
    south, north = max(-90.0, lats.min() - dlat / 2), min(90.0, lats.max() + dlat / 2)
    if east - west >= 360 - dlon / 2:
        west, east = -180.0, 180.0
    elif lon360:
        if west >= 180:
            west, east = west - 360, east - 360
        elif east > 180:
            # Crosses the antimeridian: Cesium takes west > east.
            east -= 360
    return [float(west), float(south), float(east), float(north)]


def _index_slice(coord, low, high):
    """Slice of a sorted 1D coordinate covering [low, high] plus a cell of padding."""
    pad = 1.5 * abs(coord[1] - coord[0])
    inside = numpy.nonzero((coord >= low - pad) & (coord <= high + pad))[0]
    if inside.size == 0:
        return None
    start, stop = int(inside.min()), int(inside.max()) + 1
    # Downsample at low zooms so a tile reads at most ~2 cells per output pixel.
    step = max(1, (stop - start) // (2 * TILE_SIZE))
    return slice(start, stop, step)


def _default_rescale(da, lat_name, lon_name):
    """2nd–98th percentile of a 2D slice, or None if it has no data."""
    chunks = dict(zip(da.dims, da.encoding.get("chunks") or da.encoding.get("chunksizes") or ()))
    values = da.isel({
        dim: _sample_indices(da.sizes[dim], chunks.get(dim)) for dim in (lat_name, lon_name)
    })
    values = values.values.astype("float64")
    values = values[numpy.isfinite(values)]
    if values.size == 0:
        return None
    low, high = numpy.percentile(values, [2, 98])
    if low == high:
        high = low + 1
    return [float(low), float(high)]


def _sample_indices(size, chunk, points=1024, chunks_per_axis=6):
    """Indexes along one axis to sample for a colour range.

    Spread evenly when the axis has few stored chunks; otherwise taken from a handful of
    chunks across it, since remote stores fetch every chunk a sample touches.
    """
    if not chunk or size // chunk <= chunks_per_axis:
        return slice(None, None, max(1, size // points))
    starts = numpy.linspace(0, size - chunk, chunks_per_axis).astype(int) // chunk * chunk
    step = max(1, chunk * chunks_per_axis // points)
    return numpy.unique(numpy.concatenate([numpy.arange(s, min(s + chunk, size), step) for s in starts]))


def _dim_info(ds, dim):
    """Labels for a non-spatial dimension, and the index shown first.

    Time axes open on their latest step; other dimensions (depth, level...) on the first.
    """
    if dim not in ds.coords:
        values = [str(i) for i in range(ds.sizes[dim])]
        return {"name": dim, "units": "", "values": values, "default": 0}
    coord = ds[dim]
    values = coord.values
    is_time = numpy.issubdtype(values.dtype, numpy.datetime64) or (
        values.dtype == object and any(hasattr(v, "isoformat") for v in values)
    )
    return {
        "name": dim,
        "units": str(coord.attrs.get("units", "")),
        "values": _labels(values),
        "default": len(values) - 1 if is_time else 0,
    }


def _labels(values):
    if numpy.issubdtype(values.dtype, numpy.datetime64):
        stamps = pandas.to_datetime(values)
        # Time axes can contain NaT where the stored value was a fill value.
        valid = stamps[~stamps.isna()]
        fmt = "%Y-%m-%d" if (valid == valid.normalize()).all() else "%Y-%m-%d %H:%M"
        labels = [
            f"(missing time #{i})" if pandas.isna(s) else s.strftime(fmt)
            for i, s in enumerate(stamps)
        ]
    elif values.dtype == object:
        # cftime dates for non-standard calendars (360_day, noleap, ...); may contain None.
        labels = [
            v.isoformat() if hasattr(v, "isoformat") else f"(missing #{i})" if v is None else str(v)
            for i, v in enumerate(values)
        ]
    else:
        labels = [f"{v:g}" if isinstance(v, (float, numpy.floating)) else str(v) for v in values]
    return labels
