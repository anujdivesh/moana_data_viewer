"""Convert vector data to GeoJSON in WGS84 (EPSG:4326), reprojecting with PROJ."""

import io
import json
import zipfile
from pathlib import Path, PurePosixPath

import shapefile
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError

WGS84 = CRS.from_epsg(4326)
SHAPEFILE_PARTS = {".shp", ".shx", ".dbf", ".prj", ".cpg"}


class ConversionError(Exception):
    pass


def to_geojson(filename, data, source_crs=None):
    """Return (FeatureCollection dict in WGS84, source CRS name) for a vector file's bytes.

    source_crs overrides any CRS declared in the file. It accepts anything PROJ
    understands: "EPSG:32760", a PROJ string or WKT.
    """
    override = parse_crs(source_crs) if source_crs else None
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".zip":
        layers = _read_zipped_shapefiles(data)
    elif suffix in (".geojson", ".json"):
        layers = [_read_geojson(data)]
    else:
        raise ConversionError(
            "Unsupported file type. Open a shapefile (.shp or .zip) or GeoJSON."
        )

    features = []
    crs_names = []
    for layer_features, declared_crs, layer_name in layers:
        crs = override or declared_crs
        if crs is None:
            # Unknown CRS: accept only if the coordinates are plausibly lon/lat.
            if not _looks_geographic(layer_features):
                raise ConversionError(
                    f"{layer_name} has no CRS (no .prj file) and its coordinates are not "
                    "longitude/latitude. Enter the source CRS, e.g. EPSG:32760."
                )
            crs = WGS84
        _reproject_features(layer_features, crs)
        features.extend(layer_features)
        crs_names.append(_crs_label(crs))

    return {"type": "FeatureCollection", "features": features}, ", ".join(
        dict.fromkeys(crs_names)
    )


def path_to_geojson(path, source_crs=None):
    """Like to_geojson, for a file on disk; a .shp is read with its sidecar files."""
    path = Path(path)
    if path.suffix.lower() != ".shp":
        return to_geojson(path.name, path.read_bytes(), source_crs)
    # Bundle the .shp with its .shx/.dbf/.prj/.cpg, in memory, as the zip reader expects.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for sidecar in path.parent.iterdir():
            if sidecar.stem == path.stem and sidecar.suffix.lower() in SHAPEFILE_PARTS:
                archive.write(sidecar, sidecar.name)
    return to_geojson(f"{path.stem}.zip", buffer.getvalue(), source_crs)


def parse_crs(value):
    try:
        return CRS.from_user_input(value.strip() if isinstance(value, str) else value)
    except CRSError as exc:
        raise ConversionError(f"Unrecognised CRS {value!r}: {exc}") from exc


def _crs_label(crs):
    epsg = crs.to_epsg()
    return f"EPSG:{epsg} ({crs.name})" if epsg else crs.name


def _read_geojson(data):
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"Invalid GeoJSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConversionError("Invalid GeoJSON: expected a JSON object.")

    if doc.get("type") == "FeatureCollection":
        features = doc.get("features") or []
    elif doc.get("type") == "Feature":
        features = [doc]
    elif "coordinates" in doc or doc.get("type") == "GeometryCollection":
        features = [{"type": "Feature", "geometry": doc, "properties": {}}]
    else:
        raise ConversionError("Invalid GeoJSON: no features or geometry found.")

    # RFC 7946 GeoJSON is always WGS84, but older (2008 spec) files may declare
    # a "crs" member such as {"type": "name", "properties": {"name": "EPSG:3857"}}.
    crs = None
    crs_member = doc.get("crs")
    if isinstance(crs_member, dict):
        name = (crs_member.get("properties") or {}).get("name")
        if name:
            crs = parse_crs(name)
    return features, crs or WGS84, "GeoJSON"


def _read_zipped_shapefiles(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConversionError("The .zip file is corrupt or not a zip.") from exc

    # Index members by lowercase path without extension, ignoring macOS junk.
    # Members are read in memory and never extracted to disk.
    parts = {}
    for member in archive.namelist():
        path = PurePosixPath(member)
        if member.endswith("/") or "__MACOSX" in path.parts or path.name.startswith("."):
            continue
        stem = str(path.with_suffix("")).lower()
        parts.setdefault(stem, {})[path.suffix.lower()] = member

    shapefiles = {stem: exts for stem, exts in parts.items() if ".shp" in exts}
    if not shapefiles:
        raise ConversionError("No .shp file found in the zip.")

    layers = []
    for stem, exts in sorted(shapefiles.items()):
        name = PurePosixPath(stem).name
        missing = [ext for ext in (".shx", ".dbf") if ext not in exts]
        if missing:
            raise ConversionError(f"{name}.shp is missing {', '.join(missing)}.")
        read = lambda ext: archive.read(exts[ext]) if ext in exts else None
        features = _read_shapefile(
            shp=read(".shp"),
            shx=read(".shx"),
            dbf=read(".dbf"),
            cpg=read(".cpg"),
            name=name,
            layer_property=len(shapefiles) > 1,
        )
        prj = read(".prj")
        crs = parse_crs(prj.decode("utf-8", "replace")) if prj else None
        layers.append((features, crs, f"{name}.shp"))
    return layers


def _read_shapefile(shp, shx, dbf, cpg, name, layer_property):
    encoding = cpg.decode("ascii", "ignore").strip() if cpg else ""
    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(shp),
            shx=io.BytesIO(shx),
            dbf=io.BytesIO(dbf),
            encoding=encoding or "utf-8",
            encodingErrors="replace",
        )
    except (shapefile.ShapefileException, LookupError) as exc:
        raise ConversionError(f"Could not read {name}.shp: {exc}") from exc

    features = []
    with reader:
        for shape_record in reader.iterShapeRecords():
            shape = shape_record.shape
            geometry = None
            if shape.shapeType != shapefile.NULL:
                geometry = dict(shape.__geo_interface__)
            properties = shape_record.record.as_dict()
            if layer_property:
                properties["_layer"] = name
            features.append(
                {"type": "Feature", "geometry": geometry, "properties": properties}
            )
    return features


def _iter_positions(coords):
    if coords and isinstance(coords[0], (int, float)):
        yield coords
    else:
        for c in coords:
            yield from _iter_positions(c)


def _iter_geometries(geometry):
    if not geometry:
        return
    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from _iter_geometries(g)
    else:
        yield geometry


def _looks_geographic(features):
    for feature in features:
        for geometry in _iter_geometries(feature.get("geometry")):
            for x, y, *_ in _iter_positions(geometry.get("coordinates") or []):
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    return False
    return True


def _reproject_features(features, crs):
    if crs.equals(WGS84, ignore_axis_order=True):
        return
    try:
        transformer = Transformer.from_crs(crs, WGS84, always_xy=True)
        for feature in features:
            for geometry in _iter_geometries(feature.get("geometry")):
                geometry["coordinates"] = _transform_coords(
                    geometry["coordinates"], transformer
                )
    except ProjError as exc:
        raise ConversionError(
            f"Could not reproject from {_crs_label(crs)} to WGS84: {exc}. "
            "Check that the source CRS is correct."
        ) from exc


def _transform_coords(coords, transformer):
    if coords and isinstance(coords[0], (int, float)):
        x, y = transformer.transform(coords[0], coords[1], errcheck=True)
        return [x, y, *coords[2:]]
    return [_transform_coords(c, transformer) for c in coords]
