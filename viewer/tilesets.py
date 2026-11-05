"""Read PMTiles archives from a local path, an http(s) URL or s3://bucket/key."""

import gzip
import io
import os
import ssl
import urllib.request
from functools import lru_cache
from urllib.parse import urlparse

import certifi
import mapbox_vector_tile
from PIL import Image, ImageDraw
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType

from .conversion import ConversionError
from .raster import EMPTY_TILE

TILE_SIZE = 256
# Vector tiles are drawn at twice the size and scaled down, to smooth the lines.
SUPERSAMPLE = 2
# One colour per vector layer in the archive.
COLORS = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6"]
CONTENT_TYPES = {
    TileType.PNG: "image/png",
    TileType.JPEG: "image/jpeg",
    TileType.WEBP: "image/webp",
    TileType.AVIF: "image/avif",
}


def parse_s3_url(url):
    parsed = urlparse(url)
    bucket, key = parsed.netloc, parsed.path.lstrip("/")
    if parsed.scheme != "s3" or not bucket or not key:
        raise ConversionError("Enter an S3 URL as s3://bucket/path/file.pmtiles.")
    return bucket, key


@lru_cache(maxsize=256)
def _read(src, offset, length):
    """Read a byte range; cached, as every tile lookup re-reads the header and directories."""
    if src.startswith("s3://"):
        bucket, key = parse_s3_url(src)
        response = _s3().get_object(
            Bucket=bucket, Key=key, Range=f"bytes={offset}-{offset + length - 1}"
        )
        return response["Body"].read()
    if src.startswith(("http://", "https://")):
        request = urllib.request.Request(
            src, headers={"Range": f"bytes={offset}-{offset + length - 1}"}
        )
        with urllib.request.urlopen(request, context=_ssl_context(), timeout=30) as response:
            if response.status != 206:
                raise ConversionError("The server does not support range requests.")
            return response.read()
    with open(src, "rb") as f:
        f.seek(offset)
        return f.read(length)


@lru_cache(maxsize=1)
def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    # Credentials come from the usual AWS settings (env vars, ~/.aws, instance role);
    # without any, read public buckets anonymously.
    session = boto3.session.Session()
    config = None if session.get_credentials() else Config(signature_version=UNSIGNED)
    return session.client("s3", config=config)


@lru_cache(maxsize=1)
def _ssl_context():
    # SSL_CERT_FILE lets networks that re-sign HTTPS (e.g. Zscaler) supply their CA bundle.
    return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or certifi.where())


def _reader(src):
    return Reader(lambda offset, length: _read(src, offset, length))


def clear_cache():
    _read.cache_clear()


def describe(src):
    """Summarise an archive for the viewer: tile type, bounds, zoom range and layers."""
    reader = _reader(src)
    try:
        header = reader.header()
        metadata = reader.metadata()
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"Could not read the PMTiles archive: {exc}") from exc

    tile_type = header["tile_type"]
    terrain = _is_terrain(metadata)
    if tile_type == TileType.UNKNOWN and not terrain:
        kind = metadata.get("format") or "unknown"
        raise ConversionError(
            f"Unsupported PMTiles content ({kind}). Supported: raster (PNG, JPEG, WebP, AVIF), "
            "vector (MVT) and quantized-mesh terrain."
        )
    if tile_type not in (TileType.UNKNOWN, TileType.MVT) and tile_type not in CONTENT_TYPES:
        raise ConversionError(f"Unsupported PMTiles tile type: {tile_type.name}.")
    if header["tile_compression"] not in (Compression.NONE, Compression.GZIP, Compression.UNKNOWN):
        raise ConversionError(
            f"Unsupported PMTiles tile compression: {header['tile_compression'].name}."
        )

    bounds = [header[k] / 1e7 for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7")]
    if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
        bounds = [-180.0, -85.0511, 180.0, 85.0511]
    if terrain:
        offset = metadata.get("zoomOffset", 1)
        return {
            "tile_type": "terrain",
            "bounds": bounds,
            "minzoom": max(header["min_zoom"] - offset, 0),
            "maxzoom": header["max_zoom"] - offset,
            "layers": [],
            "crs": "EPSG:4326",
        }
    return {
        "tile_type": tile_type.name.lower(),
        "bounds": bounds,
        "minzoom": header["min_zoom"],
        "maxzoom": header["max_zoom"],
        # Vector tiles are drawn at 256 px; raster archives say their own size.
        "tile_size": TILE_SIZE if tile_type == TileType.MVT else int(metadata.get("tileSize", 256)),
        "layers": [layer.get("id") for layer in metadata.get("vector_layers", [])],
        "crs": "EPSG:3857",
    }


def _is_terrain(metadata):
    """Quantized-mesh terrain, as packed by e.g. lidar-acrobatics' build_cesium_terrain.py."""
    return (
        metadata.get("format") == "quantized-mesh-1.0"
        and metadata.get("scheme") == "cesium-geographic-tms"
        and isinstance(metadata.get("layer"), dict)
    )


def terrain_layer(src):
    """The layer.json Cesium's terrain loader starts from; tiles are relative to it."""
    layer = dict(_reader(src).metadata()["layer"])
    layer["tiles"] = ["{z}/{x}/{y}.terrain"]
    return layer


def terrain_tile(src, z, x, y):
    """One quantized-mesh tile, addressed as Cesium asks for it (geographic, TMS rows)."""
    reader = _reader(src)
    data = reader.get(z + reader.metadata().get("zoomOffset", 1), x, y)
    if data and data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def render_tile(src, z, x, y):
    """Return (bytes, content type) for one tile; vector tiles are drawn to PNG."""
    reader = _reader(src)
    header = reader.header()
    data = reader.get(z, x, y) if header["min_zoom"] <= z <= header["max_zoom"] else None
    if not data or header["tile_type"] == TileType.UNKNOWN:
        return EMPTY_TILE, "image/png"
    if header["tile_compression"] == Compression.GZIP or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if header["tile_type"] == TileType.MVT:
        return _draw_mvt(data), "image/png"
    return data, CONTENT_TYPES[header["tile_type"]]


def _draw_mvt(data):
    size = TILE_SIZE * SUPERSAMPLE
    image = Image.new("RGBA", (size, size))
    layers = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    for i, layer in enumerate(layers.values()):
        color = COLORS[i % len(COLORS)]
        scale = size / layer.get("extent", 4096)

        def px(coords):
            return [(cx * scale, cy * scale) for cx, cy, *_ in coords]

        fill = Image.new("L", (size, size))
        fill_draw = ImageDraw.Draw(fill)
        lines = ImageDraw.Draw(image)
        for feature in layer["features"]:
            geometry = feature["geometry"]
            kind, coords = geometry["type"], geometry["coordinates"]
            if kind in ("Polygon", "MultiPolygon"):
                for polygon in [coords] if kind == "Polygon" else coords:
                    for ring_index, ring in enumerate(polygon):
                        if len(ring) < 3:
                            continue
                        # Outer ring filled, holes cut out.
                        fill_draw.polygon(px(ring), fill=0 if ring_index else 255)
                        lines.line(px(ring) + px(ring[:1]), fill=color, width=2 * SUPERSAMPLE)
            elif kind in ("LineString", "MultiLineString"):
                for line in [coords] if kind == "LineString" else coords:
                    lines.line(px(line), fill=color, width=2 * SUPERSAMPLE, joint="curve")
            elif kind in ("Point", "MultiPoint"):
                r = 3 * SUPERSAMPLE
                for cx, cy in px([coords] if kind == "Point" else coords):
                    lines.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
        # Polygon fills at 40% opacity, under the outlines already drawn.
        tint = Image.new("RGBA", (size, size), color)
        tint.putalpha(fill.point(lambda v: v * 0.4))
        image = Image.alpha_composite(tint, image)

    out = io.BytesIO()
    image.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()
