"""Remote datasets (http/https, s3://): tell what a URL holds, and fetch small files."""

import io
import urllib.request
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse, urlunparse

from .conversion import SHAPEFILE_PARTS, ConversionError, to_geojson
from .datafiles import SUFFIX_KINDS
from .tilesets import _s3, _ssl_context, parse_s3_url

KINDS = ("vector", "raster", "zarr", "netcdf", "pointcloud", "pmtiles")
# What a file starts with, for URLs whose name doesn't say.
MAGIC = [
    (b"PMTiles", "pmtiles"),
    (b"II*\x00", "raster"),
    (b"MM\x00*", "raster"),
    (b"II+\x00", "raster"),  # BigTIFF
    (b"MM\x00+", "raster"),
    (b"LASF", "pointcloud"),
    (b"CDF\x01", "netcdf"),
    (b"CDF\x02", "netcdf"),
    (b"\x89HDF", "netcdf"),
    (b"PK\x03\x04", "vector"),  # zipped shapefile
]
ZARR_METADATA = (".zmetadata", ".zgroup", "zarr.json")
# GeoJSON and shapefiles are fetched whole; larger files should be opened from disk.
MAX_VECTOR_BYTES = 500 * 1024 * 1024
TIMEOUT = 30


def is_url(value):
    return urlparse(value).scheme in ("http", "https", "s3")


def https_url(url):
    """s3://bucket/key as its public https address, for readers without S3 support."""
    if not url.startswith("s3://"):
        return url
    bucket, key = parse_s3_url(url)
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def kind_for_url(url, hint=None):
    """The layer kind a URL opens as: the hint, else its name, else its first bytes."""
    if hint:
        if hint not in KINDS:
            raise ConversionError(f"Unknown type {hint!r}; use one of {', '.join(KINDS)}.")
        return hint
    kind = kind_from_name(url)
    if kind:
        return kind

    head = read_head(url)
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind
    if head.lstrip()[:1] in (b"{", b"["):
        return "vector"
    # A Zarr store is a folder: nothing at the URL itself, metadata files inside it.
    base = url.rstrip("/")
    if any(read_head(f"{base}/{name}") for name in ZARR_METADATA):
        return "zarr"
    raise ConversionError(
        f"Couldn't tell what kind of data {url} is. Add a type, e.g. &type=netcdf "
        f"(one of {', '.join(KINDS)})."
    )


def kind_from_name(url):
    """The layer kind a URL's name says it is, or None."""
    path = urlparse(url).path.lower().rstrip("/")
    # THREDDS / Hyrax OPeNDAP endpoints serve NetCDF, whatever the name ends with.
    if "/dodsc/" in path or "/opendap/" in path:
        return "netcdf"
    if path.endswith(".zarr") or ".zarr/" in path:
        return "zarr"
    if path.endswith(".copc.laz"):
        return "pointcloud"
    return SUFFIX_KINDS.get(PurePosixPath(path).suffix)


def is_s3_folder(url):
    return url.startswith("s3://") and url.endswith("/") and not kind_from_name(url)


def list_s3(url, limit=1000):
    """An s3://bucket/prefix/ "folder", shaped like datafiles.listing: sub-prefixes and the
    objects the viewer can show (a .zarr prefix counts as one dataset)."""
    parsed = urlparse(url)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    if not bucket:
        raise ConversionError("Enter an S3 folder as s3://bucket/prefix/.")
    entries = []
    truncated = False
    try:
        pages = _s3().get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, Delimiter="/"
        )
        for page in pages:
            for common in page.get("CommonPrefixes", []):
                sub = f"s3://{bucket}/{common['Prefix']}"
                kind = kind_from_name(sub)
                name = common["Prefix"][len(prefix):].rstrip("/")
                entries.append({"name": name, "path": sub, "dir": kind is None, "kind": kind})
            for obj in page.get("Contents", []):
                obj_url = f"s3://{bucket}/{obj['Key']}"
                kind = kind_from_name(obj_url)
                if kind and obj["Key"] != prefix:
                    entries.append({"name": obj["Key"][len(prefix):], "path": obj_url, "dir": False, "kind": kind})
            if len(entries) > limit:
                truncated = True
                break
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Could not list {url}: {exc}") from exc
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent_prefix = prefix.rstrip("/").rpartition("/")[0]
    return {
        "path": url,
        "parent": f"s3://{bucket}/{parent_prefix + '/' if parent_prefix else ''}" if prefix else None,
        "entries": entries[:limit],
        "truncated": truncated,
    }


def vector_to_geojson(url, source_crs=None):
    """Like conversion.path_to_geojson, for GeoJSON or a shapefile at a URL."""
    parsed = urlparse(url)
    name = PurePosixPath(parsed.path).name
    suffix = PurePosixPath(name).suffix
    if suffix.lower() == ".shp":
        # Fetch the .shx/.dbf/.prj/.cpg that sit next to it, as for a .shp on disk.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for part in sorted(SHAPEFILE_PARTS):
                part = part.upper() if suffix.isupper() else part
                sidecar = urlunparse(parsed._replace(path=parsed.path[: -len(suffix)] + part))
                try:
                    archive.writestr(PurePosixPath(name).stem + part, read_all(sidecar))
                except ConversionError:
                    raise
                except Exception:
                    if part.lower() in (".shp", ".shx", ".dbf"):
                        raise ConversionError(f"Could not fetch {sidecar}.") from None
        return to_geojson(f"{PurePosixPath(name).stem}.zip", buffer.getvalue(), source_crs)

    try:
        data = read_all(url)
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Could not fetch {url}: {exc}") from exc
    if suffix.lower() not in (".zip", ".geojson", ".json"):
        # e.g. a WFS or API query: go by what came back.
        name = "data.zip" if data.startswith(b"PK\x03\x04") else "data.geojson"
    return to_geojson(name, data, source_crs)


def read_head(url, length=16):
    """The first bytes at url, or b"" if there is nothing there."""
    try:
        return _get(url, f"bytes=0-{length - 1}")[:length]
    except Exception:  # missing, forbidden, unreachable: nothing to recognise
        return b""


def read_all(url):
    data = _get(url)
    if len(data) > MAX_VECTOR_BYTES:
        raise ConversionError(
            f"{url} is larger than {MAX_VECTOR_BYTES // 1024**2} MB; open it from disk instead."
        )
    return data


def _get(url, byte_range=None):
    if url.startswith("s3://"):
        bucket, key = parse_s3_url(url)
        extra = {"Range": byte_range} if byte_range else {}
        return _s3().get_object(Bucket=bucket, Key=key, **extra)["Body"].read(MAX_VECTOR_BYTES + 1)
    headers = {"Range": byte_range} if byte_range else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, context=_ssl_context(), timeout=TIMEOUT) as response:
        return response.read(MAX_VECTOR_BYTES + 1)
