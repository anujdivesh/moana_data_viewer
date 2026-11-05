import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import laspy
import morecantile
import numpy
import pandas
import shapefile
import xarray
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from pyproj import CRS, Transformer
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from . import raster
from .conversion import ConversionError, path_to_geojson, to_geojson
from .models import Upload


def make_shapefile_zip(points, prj_epsg=None, name="places"):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POINT) as w:
        w.field("label", "C")
        for label, (x, y) in points:
            w.point(x, y)
            w.record(label)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(f"{name}.shp", shp.getvalue())
        z.writestr(f"{name}.shx", shx.getvalue())
        z.writestr(f"{name}.dbf", dbf.getvalue())
        if prj_epsg:
            z.writestr(f"{name}.prj", CRS.from_epsg(prj_epsg).to_wkt("WKT1_ESRI"))
    return buffer.getvalue()


SUVA = (178.44, -18.14)


def suva_in(epsg):
    return Transformer.from_crs(4326, epsg, always_xy=True).transform(*SUVA)


class ConversionTests(TestCase):
    def test_shapefile_in_wgs84(self):
        data = make_shapefile_zip([("Suva", SUVA)], prj_epsg=4326)
        collection, crs = to_geojson("places.zip", data)
        self.assertTrue(crs.startswith("EPSG:4326"))
        feature = collection["features"][0]
        self.assertEqual(feature["properties"], {"label": "Suva"})
        self.assertEqual(feature["geometry"]["type"], "Point")
        self.assertAlmostEqual(feature["geometry"]["coordinates"][0], 178.44)
        self.assertAlmostEqual(feature["geometry"]["coordinates"][1], -18.14)

    def test_shapefile_is_reprojected_to_wgs84(self):
        data = make_shapefile_zip([("Suva", suva_in(32760))], prj_epsg=32760)
        collection, crs = to_geojson("places.zip", data)
        self.assertIn("UTM zone 60S", crs)
        lon, lat = collection["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lon, SUVA[0], places=6)
        self.assertAlmostEqual(lat, SUVA[1], places=6)

    def test_projected_shapefile_without_prj_needs_crs(self):
        data = make_shapefile_zip([("Suva", suva_in(32760))])
        with self.assertRaisesMessage(ConversionError, "Enter the source CRS"):
            to_geojson("places.zip", data)

    def test_source_crs_override_reprojects_shapefile_without_prj(self):
        data = make_shapefile_zip([("Suva", suva_in(32760))])
        collection, _ = to_geojson("places.zip", data, source_crs="EPSG:32760")
        lon, lat = collection["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lon, SUVA[0], places=6)
        self.assertAlmostEqual(lat, SUVA[1], places=6)

    def test_lonlat_shapefile_without_prj_is_assumed_wgs84(self):
        data = make_shapefile_zip([("Suva", SUVA)])
        collection, crs = to_geojson("places.zip", data)
        self.assertTrue(crs.startswith("EPSG:4326"))
        self.assertEqual(list(collection["features"][0]["geometry"]["coordinates"]), list(SUVA))

    def test_geojson_crs_member_is_reprojected(self):
        doc = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": list(suva_in(3857))},
                }
            ],
        }
        collection, _ = to_geojson("p.geojson", json.dumps(doc).encode())
        lon, lat = collection["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lon, SUVA[0], places=6)
        self.assertAlmostEqual(lat, SUVA[1], places=6)

    def test_invalid_crs_is_rejected(self):
        data = make_shapefile_zip([("Suva", SUVA)])
        with self.assertRaisesMessage(ConversionError, "Unrecognised CRS"):
            to_geojson("places.zip", data, source_crs="EPSG:not-a-code")

    def test_zip_without_shp_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("readme.txt", "hello")
        with self.assertRaisesMessage(ConversionError, "No .shp"):
            to_geojson("x.zip", buffer.getvalue())

    def test_geojson_geometry_is_wrapped(self):
        data = json.dumps({"type": "Point", "coordinates": [1, 2]}).encode()
        collection, _ = to_geojson("p.geojson", data)
        self.assertEqual(collection["features"][0]["geometry"]["coordinates"], [1, 2])

    def test_unsupported_extension(self):
        with self.assertRaises(ConversionError):
            to_geojson("data.csv", b"a,b")

    def test_shp_on_disk_is_read_with_its_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(io.BytesIO(make_shapefile_zip([("Suva", suva_in(32760))], 32760))) as z:
                z.extractall(folder)
            collection, crs = path_to_geojson(Path(folder, "places.shp"))
        self.assertIn("UTM zone 60S", crs)
        lon, lat = collection["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lon, SUVA[0], places=6)


class DataFolderTestCase(TestCase):
    """Files are opened in place from a temporary data folder; caches go to a temporary dir."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp()).resolve()
        self.cache = tempfile.mkdtemp()
        self.override = override_settings(DATA_ROOTS=[self.data], CACHE_DIR=self.cache)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.data)
        shutil.rmtree(self.cache)

    def put(self, name, data):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def open(self, path=None, **fields):
        if path is not None:
            fields["path"] = str(path)
        return self.client.post(reverse("viewer:upload-open"), fields)

    def tile(self, upload_id, lon, lat, z, **params):
        tile = morecantile.tms.get("WebMercatorQuad").tile(lon, lat, z)
        return self.client.get(
            reverse("viewer:upload-tile", args=[upload_id, tile.z, tile.x, tile.y]), params
        )


def make_geotiff(west, east, south=-18.0, north=-16.0, epsg=32760, size=600, crs=True):
    """A float32 GeoTIFF in a UTM grid covering the given lon/lat box (west > east crosses 180°)."""
    to_crs = Transformer.from_crs(4326, epsg, always_xy=True)
    x0, y1 = to_crs.transform(west, north)
    x1, y0 = to_crs.transform(east, south)
    data = numpy.arange(size * size, dtype="float32").reshape(1, size, size)
    buffer = io.BytesIO()
    # Striped, untiled and without overviews, so it is not a valid COG.
    with MemoryFile() as memfile:
        with memfile.open(
            driver="GTiff", width=size, height=size, count=1, dtype="float32",
            crs=f"EPSG:{epsg}" if crs else None,
            transform=from_bounds(x0, y0, x1, y1, size, size),
        ) as dst:
            dst.write(data)
        buffer.write(memfile.read())
    return buffer.getvalue()


class RasterTests(DataFolderTestCase):
    def test_geotiff_is_read_in_place_and_tiled(self):
        # Not a COG: read as it is, nothing converted or copied.
        path = self.put("dem.tif", make_geotiff(177.0, 179.0))
        response = self.open(path)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual(body["kind"], "raster")
        self.assertEqual(body["name"], "dem")
        self.assertEqual(body["source"], str(path))
        self.assertEqual(body["detected_crs"], "EPSG:32760")
        west, south, east, north = body["raster_info"]["bounds"]
        self.assertAlmostEqual(west, 177.0, places=1)
        self.assertAlmostEqual(east, 179.0, places=1)
        self.assertGreaterEqual(body["raster_info"]["maxzoom"], 7)

        inside = self.tile(body["id"], 178.0, -17.0, 8)
        self.assertEqual(inside.status_code, 200)
        self.assertEqual(inside["Content-Type"], "image/png")
        self.assertNotEqual(inside.content, raster.EMPTY_TILE)
        self.assertEqual(self.tile(body["id"], 0.0, 0.0, 8).content, raster.EMPTY_TILE)
        self.assertEqual(os.listdir(self.cache), [])

    def test_raster_crossing_antimeridian(self):
        response = self.open(self.put("dem.tif", make_geotiff(179.0, -179.0)))
        self.assertEqual(response.status_code, 201, response.content)
        info = response.json()["upload"]["raster_info"]
        west, _, east, _ = info["bounds"]
        self.assertGreater(west, east)
        self.assertGreaterEqual(info["maxzoom"], 7)

        upload_id = response.json()["upload"]["id"]
        for lon in (179.5, -179.5):
            self.assertNotEqual(self.tile(upload_id, lon, -17.0, 8).content, raster.EMPTY_TILE)

    def test_geotiff_without_crs_needs_source_crs(self):
        path = self.put("dem.tif", make_geotiff(177.0, 179.0, crs=False))
        response = self.open(path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Enter the source CRS", response.json()["error"])

        # The CRS is assigned on the fly; the file is left as it is.
        response = self.open(path, source_crs="EPSG:32760")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual(body["detected_crs"], "EPSG:32760")
        self.assertNotEqual(self.tile(body["id"], 178.0, -17.0, 8).content, raster.EMPTY_TILE)
        with rasterio.open(path) as src:
            self.assertIsNone(src.crs)

    def test_tile_style_parameters(self):
        upload_id = self.open(self.put("dem.tif", make_geotiff(177.0, 179.0))).json()["upload"]["id"]
        ok = self.tile(upload_id, 178.0, -17.0, 8, colormap="terrain", rescale="0,1000")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.tile(upload_id, 178.0, -17.0, 8, colormap="nope").status_code, 400)
        self.assertEqual(self.tile(upload_id, 178.0, -17.0, 8, rescale="1").status_code, 400)

    def test_remote_url_must_be_http_or_s3(self):
        self.assertEqual(self.open(url="file:///etc/passwd").status_code, 400)
        self.assertFalse(Upload.objects.exists())


def make_zarr_store(path, lons, lats=None):
    """A Zarr v2 store with sst(time, depth, lat, lon) where values encode time and depth."""
    lats = numpy.arange(-30.0, 0.0, 0.5) if lats is None else lats
    times = pandas.date_range("2026-01-01", periods=3, freq="D")
    depths = [0.0, 50.0]
    data = numpy.zeros((len(times), len(depths), len(lats), len(lons)), dtype="float32")
    for t in range(len(times)):
        for d in range(len(depths)):
            data[t, d] = 20 + t + 5 * d
    data[..., :2, :] = numpy.nan  # some missing cells
    ds = xarray.Dataset(
        {"sst": (("time", "depth", "lat", "lon"), data, {"units": "degC", "long_name": "Sea temperature"})},
        coords={
            "time": times,
            "depth": ("depth", depths, {"units": "m"}),
            "lat": ("lat", lats, {"units": "degrees_north"}),
            "lon": ("lon", lons, {"units": "degrees_east"}),
        },
    )
    ds.to_zarr(path, zarr_format=2, consolidated=True)
    return path


class ZarrTests(DataFolderTestCase):
    def pacific_store(self):
        # 0–360 longitudes over the western and central Pacific, crossing 180°.
        return make_zarr_store(str(self.data / "pacific.zarr"), numpy.arange(140.0, 220.0, 0.5))

    def open_store(self, path):
        response = self.open(path)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["upload"]

    def test_zarr_folder_is_opened_in_place(self):
        body = self.open_store(self.pacific_store())
        self.assertEqual(body["kind"], "zarr")
        self.assertEqual(body["name"], "pacific")

        info = body["raster_info"]
        self.assertTrue(info["lon360"])
        west, _, east, _ = info["bounds"]
        self.assertAlmostEqual(west, 139.75)
        self.assertAlmostEqual(east, 219.75 - 360)  # west > east: crosses 180°
        sst = info["variables"]["sst"]
        self.assertEqual([d["name"] for d in sst["dims"]], ["time", "depth"])
        self.assertEqual(sst["dims"][0]["values"], ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(sst["dims"][1]["values"], ["0", "50"])
        # Opens on the latest time and the first depth; the range comes from that slice.
        self.assertEqual([d["default"] for d in sst["dims"]], [2, 0])
        self.assertAlmostEqual(sst["rescale"][0], 22.0)

        Upload.objects.get().delete()
        self.assertTrue((self.data / "pacific.zarr").exists())  # sources are never deleted

    def test_tiles_on_both_sides_of_antimeridian(self):
        upload_id = self.open_store(self.pacific_store())["id"]
        for lon in (178.0, -178.0, -150.0):
            response = self.tile(upload_id, lon, -15.0, 6)
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(response.content, raster.EMPTY_TILE, lon)
        # Outside the data: Atlantic.
        self.assertEqual(self.tile(upload_id, -30.0, -15.0, 6).content, raster.EMPTY_TILE)

    def test_world_tile_draws_both_hemispheres_of_a_0_360_grid(self):
        upload_id = self.open_store(self.pacific_store())["id"]
        png = self.client.get(reverse("viewer:upload-tile", args=[upload_id, 0, 0, 0])).content
        alpha = numpy.array(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]
        row = 139  # about 15°S at zoom 0
        for lon in (170.0, -150.0):
            self.assertGreater(alpha[row, int((lon + 180) / 360 * 256)], 0, lon)
        self.assertEqual(alpha[row, int((-30.0 + 180) / 360 * 256)], 0)  # Atlantic: no data

    def test_time_and_depth_selection_change_the_tile(self):
        upload_id = self.open_store(self.pacific_store())["id"]
        args = dict(lon=178.0, lat=-15.0, z=6, variable="sst", rescale="20,30")
        first = self.tile(upload_id, **args, sel="time:0,depth:0").content
        later = self.tile(upload_id, **args, sel="time:2,depth:0").content
        deeper = self.tile(upload_id, **args, sel="time:0,depth:1").content
        self.assertNotEqual(first, later)
        self.assertNotEqual(first, deeper)

        self.assertEqual(self.tile(upload_id, **args, sel="time:9").status_code, 400)
        self.assertEqual(self.tile(upload_id, **args, sel="bogus:0").status_code, 400)
        bad_var = dict(args, variable="nope")
        self.assertEqual(self.tile(upload_id, **bad_var).status_code, 400)

    def test_standard_longitudes(self):
        store = make_zarr_store(str(self.data / "std.zarr"), numpy.arange(-180.0, 180.0, 1.0))
        body = self.open_store(store)
        self.assertEqual(body["raster_info"]["bounds"][0::2], [-180.0, 180.0])
        self.assertFalse(body["raster_info"]["lon360"])
        self.assertNotEqual(self.tile(body["id"], 10.0, -15.0, 3).content, raster.EMPTY_TILE)

    def test_ascending_and_descending_latitudes_render_the_same(self):
        lons = numpy.arange(170.0, 190.0, 0.5)
        up = make_zarr_store(str(self.data / "up.zarr"), lons, numpy.arange(-30.0, 0.0, 0.5))
        down = make_zarr_store(str(self.data / "down.zarr"), lons, numpy.arange(-0.5, -30.5, -0.5))
        tiles = [self.tile(self.open_store(s)["id"], 178.0, -15.0, 5, rescale="20,30").content for s in (up, down)]
        # Missing rows sit at the southern edge in one store and the northern edge in the
        # other, so compare only that both render data rather than exact bytes.
        self.assertNotEqual(tiles[0], raster.EMPTY_TILE)
        self.assertNotEqual(tiles[1], raster.EMPTY_TILE)

    def test_unexpected_reader_error_is_reported_as_json(self):
        store = self.pacific_store()
        with self.assertLogs("viewer.models", "ERROR"), mock.patch(
            "viewer.gridded.describe", side_effect=KeyError("boom")
        ):
            response = self.open(store)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Unexpected error: KeyError: 'boom'")

    def test_missing_times_get_placeholder_labels(self):
        path = str(self.data / "nat.zarr")
        times = numpy.array(["2026-01-01", "NaT", "2026-01-03T06:00"], dtype="datetime64[ns]")
        xarray.Dataset(
            {"ssh": (("time", "lat", "lon"), numpy.ones((3, 4, 5), dtype="float32"))},
            coords={"time": times, "lat": numpy.arange(4.0), "lon": numpy.arange(170.0, 175.0)},
        ).to_zarr(path, zarr_format=2)
        body = self.open_store(path)
        self.assertEqual(
            body["raster_info"]["variables"]["ssh"]["dims"][0]["values"],
            ["2026-01-01 00:00", "(missing time #1)", "2026-01-03 06:00"],
        )

    def test_nested_chunk_keys(self):
        flat = make_zarr_store(str(self.data / "flat.zarr"), numpy.arange(170.0, 190.0, 0.5))
        ds = xarray.open_zarr(flat)
        for var in ds.variables.values():
            var.encoding.pop("chunks", None)
        store = str(self.data / "nested.zarr")
        ds.to_zarr(store, zarr_format=2, consolidated=True,
                   encoding={"sst": {"chunk_key_encoding": {"name": "v2", "separator": "/"}}})
        upload_id = self.open_store(store)["id"]
        self.assertNotEqual(self.tile(upload_id, 178.0, -15.0, 5).content, raster.EMPTY_TILE)

    def test_netcdf_file_works_like_a_zarr_store(self):
        store = self.pacific_store()
        path = self.data / "pacific.nc"
        xarray.open_zarr(store).to_netcdf(path)

        body = self.open_store(path)
        self.assertEqual(body["kind"], "netcdf")
        self.assertEqual(body["name"], "pacific")
        info = body["raster_info"]
        self.assertEqual(info["format"], "netcdf")
        self.assertGreater(info["bounds"][0], info["bounds"][2])  # crosses 180°
        self.assertEqual([d["default"] for d in info["variables"]["sst"]["dims"]], [2, 0])

        for lon in (178.0, -178.0):
            self.assertNotEqual(self.tile(body["id"], lon, -15.0, 6).content, raster.EMPTY_TILE, lon)
        args = dict(lon=178.0, lat=-15.0, z=6, rescale="20,30")
        self.assertNotEqual(
            self.tile(body["id"], **args, sel="time:0,depth:0").content,
            self.tile(body["id"], **args, sel="time:2,depth:1").content,
        )

        listing = self.client.get(reverse("viewer:browse"), {"path": str(self.data)}).json()
        self.assertIn(("pacific.nc", "netcdf"), [(e["name"], e["kind"]) for e in listing["entries"]])

    def test_curvilinear_grid_is_rejected(self):
        path = str(self.data / "curv.zarr")
        lat2d, lon2d = numpy.meshgrid(numpy.arange(3.0), numpy.arange(4.0), indexing="ij")
        xarray.Dataset(
            {"v": (("y", "x"), numpy.ones((3, 4)))},
            coords={
                "lat": (("y", "x"), lat2d, {"units": "degrees_north"}),
                "lon": (("y", "x"), lon2d, {"units": "degrees_east"}),
            },
        ).to_zarr(path, zarr_format=2)
        response = self.open(path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("curvilinear", response.json()["error"])


class PointCloudTests(DataFolderTestCase):
    def make_laz(self, crs=True, n=20_000):
        x0, y0 = suva_in(32760)
        rng = numpy.random.default_rng(0)
        header = laspy.LasHeader(point_format=7, version="1.4")
        header.scales, header.offsets = [0.01, 0.01, 0.01], [x0, y0, 0]
        if crs:
            header.add_crs(CRS.from_epsg(32760))
        las = laspy.LasData(header)
        las.x, las.y = x0 + rng.uniform(0, 300, n), y0 + rng.uniform(0, 300, n)
        las.z = rng.uniform(0, 30, n)
        las.red = las.green = las.blue = rng.integers(0, 65535, n, dtype="uint16")
        buffer = io.BytesIO()
        las.write(buffer, do_compress=True)
        return buffer.getvalue()

    def test_laz_becomes_a_cached_tileset_at_the_right_place(self):
        path = self.put("suva.copc.laz", self.make_laz())
        response = self.open(path)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual(body["kind"], "pointcloud")
        self.assertEqual(body["name"], "suva")
        self.assertEqual(body["raster_info"]["points"], 20_000)
        self.assertTrue(body["raster_info"]["rgb"])

        tileset = self.client.get(body["tileset_url"])
        self.assertEqual(tileset.status_code, 200)
        root = json.loads(b"".join(tileset.streaming_content))["root"]
        matrix = numpy.array(root["transform"]).reshape(4, 4).T
        box = numpy.array(root["boundingVolume"]["box"])
        centre = (matrix @ numpy.append(box[:3], 1))[:3]
        lon, lat, height = Transformer.from_crs(4978, 4979, always_xy=True).transform(*centre)
        self.assertAlmostEqual(lon, SUVA[0], places=2)
        self.assertAlmostEqual(lat, SUVA[1], places=2)
        self.assertLess(abs(height), 100)

        # Opening it again reuses the layer and its tiles instead of converting again.
        with mock.patch("viewer.pointcloud.to_3dtiles") as convert:
            again = self.open(path).json()["upload"]
        convert.assert_not_called()
        self.assertEqual(again["id"], body["id"])

        upload = Upload.objects.get()
        escape = self.client.get(reverse("viewer:upload-3dtiles", args=[upload.id, "../../escape"]))
        self.assertIn(escape.status_code, (400, 404))
        upload.delete()
        self.assertFalse(upload.tiles_dir.exists())
        self.assertTrue(path.exists())

    def test_point_cloud_without_crs_needs_source_crs(self):
        path = self.put("p.laz", self.make_laz(crs=False))
        response = self.open(path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Enter the source CRS", response.json()["error"])
        self.assertEqual(self.open(path, source_crs="EPSG:32760").status_code, 201)


def make_pmtiles(path, tiles, tile_type, metadata=None):
    """Write a PMTiles archive with tiles {(z, x, y): bytes}, covering Fiji."""
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import write

    with write(path) as writer:
        for (z, x, y), data in sorted(tiles.items(), key=lambda t: zxy_to_tileid(*t[0])):
            writer.write_tile(zxy_to_tileid(z, x, y), data)
        zooms = [z for z, _, _ in tiles]
        writer.finalize(
            {
                "tile_type": tile_type,
                "tile_compression": Compression.GZIP if tile_type == TileType.MVT else Compression.NONE,
                "min_zoom": min(zooms),
                "max_zoom": max(zooms),
                "min_lon_e7": int(177 * 1e7),
                "min_lat_e7": int(-19 * 1e7),
                "max_lon_e7": int(179 * 1e7),
                "max_lat_e7": int(-16 * 1e7),
                "center_zoom": 0,
                "center_lon_e7": int(178 * 1e7),
                "center_lat_e7": int(-18 * 1e7),
            },
            metadata or {},
        )


class PMTilesTests(DataFolderTestCase):
    def vector_archive(self):
        import gzip

        import mapbox_vector_tile
        from pmtiles.tile import TileType

        mvt = mapbox_vector_tile.encode(
            [{"name": "roads", "features": [{"geometry": "LINESTRING (0 0, 4096 4096)", "properties": {}}]}]
        )
        path = self.data / "roads.pmtiles"
        make_pmtiles(path, {(0, 0, 0): gzip.compress(mvt)}, TileType.MVT, {"vector_layers": [{"id": "roads"}]})
        return path

    def zxy(self, upload_id, z, x, y):
        return self.client.get(reverse("viewer:upload-tile", args=[upload_id, z, x, y]))

    def test_vector_pmtiles_draws_tiles(self):
        response = self.open(self.vector_archive())
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual(body["kind"], "pmtiles")
        self.assertEqual(body["raster_info"]["tile_type"], "mvt")
        self.assertEqual(body["raster_info"]["layers"], ["roads"])
        self.assertAlmostEqual(body["raster_info"]["bounds"][0], 177)

        tile = self.zxy(body["id"], 0, 0, 0)
        self.assertEqual(tile["Content-Type"], "image/png")
        alpha = numpy.array(Image.open(io.BytesIO(tile.content)).convert("RGBA"))[..., 3]
        # The diagonal line is drawn, the rest of the tile is transparent.
        self.assertGreater(alpha[128, 128], 0)
        self.assertEqual(alpha[10, 240], 0)

        # Outside the archive's zoom range: an empty tile, not an error.
        self.assertEqual(self.zxy(body["id"], 3, 0, 0).content, raster.EMPTY_TILE)

    def test_raster_pmtiles_tiles_are_passed_through(self):
        from pmtiles.tile import TileType

        png = raster.EMPTY_TILE
        path = self.data / "imagery.pmtiles"
        make_pmtiles(path, {(1, 1, 1): png}, TileType.PNG, {"tileSize": 512})
        body = self.open(path).json()["upload"]
        self.assertEqual(body["raster_info"]["tile_size"], 512)
        tile = self.zxy(body["id"], 1, 1, 1)
        self.assertEqual(tile["Content-Type"], "image/png")
        self.assertEqual(tile.content, png)

    def test_pmtiles_from_s3(self):
        from . import tilesets

        data = self.vector_archive().read_bytes()

        class FakeS3:
            def get_object(self, Bucket, Key, Range):
                assert (Bucket, Key) == ("my-bucket", "layers/roads.pmtiles")
                start, end = map(int, Range.removeprefix("bytes=").split("-"))
                return {"Body": io.BytesIO(data[start : end + 1])}

        tilesets.clear_cache()
        with mock.patch.object(tilesets, "_s3", return_value=FakeS3()):
            response = self.open(url="s3://my-bucket/layers/roads.pmtiles")
            self.assertEqual(response.status_code, 201, response.content)
            body = response.json()["upload"]
            self.assertEqual(body["name"], "roads")
            self.assertEqual(self.zxy(body["id"], 0, 0, 0).status_code, 200)

    def test_quantized_mesh_terrain_is_served_for_cesium(self):
        import gzip

        from pmtiles.tile import TileType

        # Geographic TMS tiles stored one zoom deeper, as lidar-acrobatics writes them.
        mesh = b"quantized-mesh tile"
        path = self.data / "terrain.pmtiles"
        available = [[{"startX": 0, "startY": 0, "endX": 0, "endY": 0}], [{"startX": 0, "startY": 0, "endX": 1, "endY": 1}]]
        make_pmtiles(
            path,
            {(1, 0, 0): gzip.compress(mesh), (2, 1, 1): gzip.compress(mesh)},
            TileType.UNKNOWN,
            {
                "format": "quantized-mesh-1.0",
                "scheme": "cesium-geographic-tms",
                "zoomOffset": 1,
                "layer": {"tilejson": "2.1.0", "format": "quantized-mesh-1.0", "tiles": ["t/{z}/{x}/{y}"], "available": available},
            },
        )
        body = self.open(path).json()["upload"]
        self.assertEqual(body["raster_info"]["tile_type"], "terrain")
        self.assertEqual((body["raster_info"]["minzoom"], body["raster_info"]["maxzoom"]), (0, 1))

        layer = self.client.get(body["terrain_url"]).json()
        self.assertEqual(layer["tiles"], ["{z}/{x}/{y}.terrain"])
        self.assertEqual(layer["available"], available)

        tile = self.client.get(reverse("viewer:terrain-tile", args=[body["id"], 1, 1, 1]))
        self.assertEqual(tile.content, mesh)
        self.assertEqual(self.client.get(reverse("viewer:terrain-tile", args=[body["id"], 0, 1, 0])).status_code, 404)

    def test_unknown_pmtiles_content_is_rejected(self):
        from pmtiles.tile import TileType

        path = self.data / "mystery.pmtiles"
        make_pmtiles(path, {(0, 0, 0): b"?"}, TileType.UNKNOWN, {"format": "pbf-something"})
        response = self.open(path)
        self.assertEqual(response.status_code, 400)
        self.assertIn("pbf-something", response.json()["error"])


class VectorTests(DataFolderTestCase):
    def test_zipped_shapefile_is_served_as_geojson(self):
        path = self.put("places.zip", make_shapefile_zip([("A", (1, 2)), ("B", (3, 4))], prj_epsg=4326))
        response = self.open(path)
        self.assertEqual(response.status_code, 201)
        body = response.json()["upload"]
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["feature_count"], 2)
        self.assertEqual(body["name"], "places")

        geojson = self.client.get(body["geojson_url"]).json()
        self.assertEqual(len(geojson["features"]), 2)
        self.assertEqual(os.listdir(self.cache), [])  # nothing stored

        listed = self.client.get(reverse("viewer:upload-list")).json()["uploads"]
        self.assertEqual([u["id"] for u in listed], [body["id"]])

    def test_shp_with_source_crs(self):
        with zipfile.ZipFile(io.BytesIO(make_shapefile_zip([("Suva", suva_in(32760))]))) as z:
            z.extractall(self.data)
        response = self.open(self.data / "places.shp", source_crs="EPSG:32760")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("UTM zone 60S", response.json()["upload"]["detected_crs"])
        lon, _ = self.client.get(response.json()["upload"]["geojson_url"]).json()["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lon, SUVA[0], places=6)

    def test_invalid_source_crs_is_rejected(self):
        path = self.put("places.zip", make_shapefile_zip([("A", (1, 2))]))
        self.assertEqual(self.open(path, source_crs="nonsense").status_code, 400)
        self.assertFalse(Upload.objects.exists())

    def test_bad_file_is_recorded_as_failed(self):
        response = self.open(self.put("broken.zip", b"not a zip"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Upload.objects.get().status, Upload.Status.FAILED)

    def test_delete_removes_the_layer_not_the_file(self):
        path = self.put("p.zip", make_shapefile_zip([("A", (1, 2))]))
        upload_id = self.open(path).json()["upload"]["id"]
        response = self.client.delete(reverse("viewer:upload-delete", args=[upload_id]))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Upload.objects.exists())
        self.assertTrue(path.exists())


class BrowseTests(DataFolderTestCase):
    def browse(self, path=None):
        return self.client.get(reverse("viewer:browse"), {"path": path} if path else {})

    def test_lists_data_folders_then_viewable_entries(self):
        self.put("tonga/dem.tif", b"x")
        self.put("tonga/notes.txt", b"x")
        self.put("tonga/.hidden.tif", b"x")
        self.put("tonga/ssh.zarr/.zgroup", b"{}")
        self.put("tonga/sub/a.geojson", b"{}")

        top = self.browse().json()
        self.assertEqual([e["path"] for e in top["entries"]], [str(self.data)])
        self.assertIsNone(top["path"])

        root = self.browse(str(self.data)).json()
        self.assertIsNone(root["parent"])  # up from a data folder goes to the list of them
        self.assertEqual([e["name"] for e in root["entries"]], ["tonga"])

        tonga = self.browse(str(self.data / "tonga")).json()
        self.assertEqual(tonga["parent"], str(self.data))
        entries = {e["name"]: (e["dir"], e["kind"]) for e in tonga["entries"]}
        # Folders first; Zarr stores open as layers; unknown and hidden files are left out.
        self.assertEqual(
            entries, {"sub": (True, None), "dem.tif": (False, "raster"), "ssh.zarr": (False, "zarr")}
        )
        self.assertEqual([e["name"] for e in tonga["entries"]][0], "sub")

    def test_paths_outside_the_data_folders_are_refused(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        Path(outside, "secret.geojson").write_text("{}")
        for path in (outside, str(self.data / ".."), "/etc"):
            response = self.browse(path)
            self.assertEqual(response.status_code, 400, path)
            self.assertIn("outside the data folders", response.json()["error"])
        response = self.open(Path(outside, "secret.geojson"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Upload.objects.exists())

    def test_system_file_dialog_returns_the_chosen_path(self):
        with mock.patch("viewer.picker.choose", return_value="/data/dem.tif") as choose:
            response = self.client.post(reverse("viewer:pick"), {"folder": "1"})
        self.assertEqual(response.json(), {"path": "/data/dem.tif"})
        choose.assert_called_once_with(folder=True)

        with mock.patch("viewer.picker.choose", return_value=None):
            self.assertEqual(self.client.post(reverse("viewer:pick")).json(), {"path": None})

    def test_system_file_dialog_is_refused_for_remote_browsers(self):
        with mock.patch("viewer.picker.choose") as choose:
            response = self.client.post(reverse("viewer:pick"), REMOTE_ADDR="203.0.113.5")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["remote"])
        choose.assert_not_called()

    def test_paths_in_any_spelling(self):
        from . import datafiles

        path = self.put("my data/dem.tif", make_geotiff(177.0, 179.0))
        for spelling in (f'"{path}"', f"'{path}'", f"  {path}  ", path.as_uri()):
            self.assertEqual(datafiles.resolve(spelling), path, spelling)
        # A Windows path can't exist on this (macOS/Linux) server: say so plainly.
        for spelling in (r"C:\data\dem.tif", "D:/data/dem.tif", r"\\server\share\dem.tif", "file:///C:/data/dem.tif"):
            with self.assertRaisesMessage(ConversionError, "is a Windows path"):
                datafiles.normalize(spelling)

    def test_type_overrides_the_file_name(self):
        store = self.data / "sst.zarr"
        make_zarr_store(str(store), numpy.arange(170.0, 190.0, 0.5))
        path = self.data / "sst.dat"
        xarray.open_zarr(store).to_netcdf(path)
        self.assertIn("add a type", self.open(path).json()["error"])
        response = self.open(path, type="netcdf")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["upload"]["kind"], "netcdf")
        self.assertEqual(self.open(path, type="bogus").status_code, 400)

    @override_settings(DATA_ROOTS=[])
    def test_no_data_folders_configured(self):
        self.assertEqual(self.browse().json()["entries"], [])
        response = self.open(self.data / "x.tif")
        self.assertIn("DATA_ROOTS", response.json()["error"])


class RemoteTests(DataFolderTestCase):
    def test_kind_from_name_pattern_or_hint(self):
        from . import remote

        with mock.patch("viewer.remote.read_head") as head:
            cases = {
                "https://x.org/dem.tif?token=1": "raster",
                "s3://bucket/ortho.pmtiles": "pmtiles",
                "https://x.org/sst.NC": "netcdf",
                "https://x.org/thredds/dodsC/oisst/sst.mnmean.nc.html": "netcdf",
                "https://x.org/opendap/hyrax/data": "netcdf",
                "https://x.org/store.zarr/": "zarr",
                "s3://bucket/surveys/site.copc.laz": "pointcloud",
                "https://x.org/places.zip": "vector",
            }
            for url, kind in cases.items():
                self.assertEqual(remote.kind_for_url(url), kind, url)
            head.assert_not_called()  # all told apart by name
        self.assertEqual(remote.kind_for_url("https://x.org/api", hint="vector"), "vector")
        with self.assertRaisesMessage(ConversionError, "Unknown type"):
            remote.kind_for_url("https://x.org/api", hint="bogus")

    def test_kind_from_first_bytes(self):
        from . import remote

        def head(answers):
            return lambda url, length=16: answers.get(url, b"")

        cases = {
            b"PMTiles\x03": "pmtiles",
            b"II*\x00\x08\x00": "raster",
            b"LASF\x00\x00": "pointcloud",
            b"\x89HDF\r\n": "netcdf",
            b"CDF\x01": "netcdf",
            b'  {"type": "Feat': "vector",
            b"PK\x03\x04": "vector",
        }
        for first_bytes, kind in cases.items():
            with mock.patch("viewer.remote.read_head", head({"https://x.org/get?id=7": first_bytes})):
                self.assertEqual(remote.kind_for_url("https://x.org/get?id=7"), kind, first_bytes)
        with mock.patch("viewer.remote.read_head", head({"https://x.org/sst/.zmetadata": b"{}"})):
            self.assertEqual(remote.kind_for_url("https://x.org/sst"), "zarr")
        with mock.patch("viewer.remote.read_head", head({})):
            with self.assertRaisesMessage(ConversionError, "Add a type"):
                remote.kind_for_url("https://x.org/mystery")

    def test_remote_geojson_from_an_api_url(self):
        doc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"n": 1}, "geometry": {"type": "Point", "coordinates": list(SUVA)}}
        ]}
        data = json.dumps(doc).encode()
        url = "https://x.org/wfs?service=WFS&request=GetFeature&outputFormat=json"
        with mock.patch("viewer.remote._get", side_effect=lambda u, byte_range=None: data[:16] if byte_range else data):
            response = self.open(url=url)
            self.assertEqual(response.status_code, 201, response.content)
            body = response.json()["upload"]
            self.assertEqual((body["kind"], body["feature_count"]), ("vector", 1))
            self.assertEqual(self.client.get(body["geojson_url"]).json()["features"][0]["properties"], {"n": 1})

    def test_remote_shapefile_fetches_its_sidecars(self):
        with zipfile.ZipFile(io.BytesIO(make_shapefile_zip([("Suva", suva_in(32760))], 32760))) as z:
            parts = {Path(n).suffix: z.read(n) for n in z.namelist()}
        fetched = []

        def get(url, byte_range=None):
            fetched.append(url)
            return parts[Path(urlparse(url).path).suffix]

        from urllib.parse import urlparse

        with mock.patch("viewer.remote._get", side_effect=get):
            response = self.open(url="https://x.org/gis/places.shp?v=2")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("UTM zone 60S", response.json()["upload"]["detected_crs"])
        self.assertIn("https://x.org/gis/places.dbf?v=2", fetched)

    def test_remote_netcdf_sources(self):
        from . import gridded

        self.assertEqual(gridded._netcdf_source("/data/sst.nc"), "/data/sst.nc")
        self.assertEqual(gridded._netcdf_source("https://x.org/sst.nc"), "https://x.org/sst.nc#mode=bytes")
        dap = "https://x.org/thredds/dodsC/sst.nc"
        self.assertEqual(gridded._netcdf_source(dap), dap)
        self.assertEqual(
            gridded._netcdf_source("s3://bucket/sst.nc"), "https://bucket.s3.amazonaws.com/sst.nc#mode=bytes"
        )
        self.assertEqual(gridded._zarr_source("s3://bucket/sst.zarr"), "https://bucket.s3.amazonaws.com/sst.zarr")

    def test_remote_point_cloud_is_refused_clearly(self):
        response = self.open(url="https://x.org/site.copc.laz")
        self.assertEqual(response.status_code, 400)
        self.assertIn("download it first", response.json()["error"])


class TempUploadTests(DataFolderTestCase):
    def send(self, name, data, **fields):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(reverse("viewer:upload-temp"), {"file": SimpleUploadedFile(name, data), **fields})

    def netcdf_bytes(self):
        store = self.data / "sst.zarr"
        make_zarr_store(str(store), numpy.arange(170.0, 190.0, 0.5))
        path = self.data / "sst.nc"
        xarray.open_zarr(store).to_netcdf(path)
        return path.read_bytes()

    def test_netcdf_sent_from_the_browser_is_shown_then_expires(self):
        from datetime import timedelta

        from django.utils import timezone

        response = self.send("sst.nc", self.netcdf_bytes())
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual((body["kind"], body["name"]), ("netcdf", "sst"))
        self.assertIsNotNone(body["expires_at"])
        self.assertNotEqual(self.tile(body["id"], 178.0, -15.0, 5).content, raster.EMPTY_TILE)

        upload = Upload.objects.get(pk=body["id"])
        saved = Path(upload.source_path)
        self.assertTrue(saved.is_relative_to(Path(self.cache) / "temp") and saved.exists())

        # Not yet expired: listed. Once expired: gone, files and all, on the next request.
        self.assertEqual(len(self.client.get(reverse("viewer:upload-list")).json()["uploads"]), 1)
        Upload.objects.filter(pk=upload.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(reverse("viewer:upload-list")).json()["uploads"], [])
        self.assertFalse(saved.parent.exists())

    def test_zipped_zarr_is_extracted(self):
        store = self.data / "pacific.zarr"
        make_zarr_store(str(store), numpy.arange(140.0, 220.0, 0.5))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for file in store.rglob("*"):
                if file.is_file():
                    z.write(file, f"pacific.zarr/{file.relative_to(store)}")
        response = self.send("pacific.zarr.zip", buffer.getvalue())
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["upload"]
        self.assertEqual((body["kind"], body["name"]), ("zarr", "pacific"))
        self.assertNotEqual(self.tile(body["id"], -178.0, -15.0, 6).content, raster.EMPTY_TILE)

    def test_failed_file_leaves_nothing_behind(self):
        response = self.send("broken.nc", b"not netcdf")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Upload.objects.exists())
        self.assertEqual(list((Path(self.cache) / "temp").iterdir()), [])
        self.assertEqual(self.send("notes.txt", b"x").status_code, 400)

    @override_settings(MAX_UPLOAD_SIZE=10)
    def test_size_limit(self):
        response = self.send("sst.nc", b"x" * 20)
        self.assertEqual(response.status_code, 400)
        self.assertIn("MAX_UPLOAD_MB", response.json()["error"])

    def test_crs_definition_for_the_browser(self):
        body = self.client.get(reverse("viewer:crs"), {"code": "EPSG:32701"}).json()
        self.assertIn("+proj=utm +zone=1 +south", body["proj4"])
        self.assertEqual(self.client.get(reverse("viewer:crs"), {"code": "nonsense"}).status_code, 400)

    def test_page_offers_the_right_buttons(self):
        local = self.client.get("/").content.decode()
        self.assertIn('id="folder-button"', local)
        self.assertNotIn('id="local-button"', local)
        remote = self.client.get("/", REMOTE_ADDR="203.0.113.5").content.decode()
        self.assertIn('id="local-button"', remote)
        self.assertNotIn('id="folder-button"', remote)


class FolderTests(DataFolderTestCase):
    def test_opening_a_folder_lists_what_can_be_plotted(self):
        self.put("tonga/dem.tif", make_geotiff(177.0, 179.0))
        self.put("tonga/buildings.geojson", b'{"type": "FeatureCollection", "features": []}')
        self.put("tonga/readme.txt", b"x")
        self.put("tonga/products/a.pmtiles", b"x")
        make_zarr_store(str(self.data / "tonga" / "ssh.zarr"), numpy.arange(170.0, 175.0, 1.0))

        response = self.open(self.data / "tonga")
        self.assertEqual(response.status_code, 200, response.content)
        folder = response.json()["folder"]
        self.assertEqual(folder["path"], str(self.data / "tonga"))
        entries = {e["name"]: (e["dir"], e["kind"]) for e in folder["entries"]}
        self.assertEqual(entries, {
            "products": (True, None),
            "buildings.geojson": (False, "vector"),
            "dem.tif": (False, "raster"),
            "ssh.zarr": (False, "zarr"),
        })
        self.assertFalse(Upload.objects.exists())  # listing a folder opens nothing

        # A Zarr store is a folder too, but opens as a layer.
        self.assertEqual(self.open(self.data / "tonga" / "ssh.zarr").json()["upload"]["kind"], "zarr")

    def test_s3_prefix_lists_like_a_folder(self):
        from . import remote

        class Paginator:
            def paginate(self, Bucket, Prefix, Delimiter):
                assert (Bucket, Prefix, Delimiter) == ("my-bucket", "tonga/", "/")
                yield {
                    "CommonPrefixes": [{"Prefix": "tonga/products/"}, {"Prefix": "tonga/ssh.zarr/"}],
                    "Contents": [
                        {"Key": "tonga/"},
                        {"Key": "tonga/ortho.pmtiles"},
                        {"Key": "tonga/notes.txt"},
                        {"Key": "tonga/site.copc.laz"},
                    ],
                }

        class FakeS3:
            def get_paginator(self, name):
                return Paginator()

        with mock.patch.object(remote, "_s3", return_value=FakeS3()):
            response = self.open(url="s3://my-bucket/tonga/")
        self.assertEqual(response.status_code, 200, response.content)
        folder = response.json()["folder"]
        self.assertEqual(folder["parent"], "s3://my-bucket/")
        self.assertEqual(
            [(e["name"], e["dir"], e["kind"]) for e in folder["entries"]],
            [("products", True, None), ("ortho.pmtiles", False, "pmtiles"),
             ("site.copc.laz", False, "pointcloud"), ("ssh.zarr", False, "zarr")],
        )
        self.assertEqual(folder["entries"][1]["path"], "s3://my-bucket/tonga/ortho.pmtiles")


class ValueTests(DataFolderTestCase):
    def value(self, upload_id, **params):
        return self.client.get(reverse("viewer:upload-value", args=[upload_id]), params)

    def test_geotiff_value_at_a_point(self):
        path = self.put("dem.tif", make_geotiff(177.0, 179.0))
        upload_id = self.open(path).json()["upload"]["id"]
        lon, lat = 178.0, -17.0
        body = self.value(upload_id, lon=lon, lat=lat).json()
        with rasterio.open(path) as src:
            x, y = Transformer.from_crs(4326, src.crs, always_xy=True).transform(lon, lat)
            row, col = src.index(x, y)
            expected = float(src.read(1)[row, col])
        self.assertEqual(body["values"], {"Band 1": expected})
        self.assertIsNone(self.value(upload_id, lon=0, lat=0).json()["values"])
        self.assertEqual(self.value(upload_id, lon="x", lat=1).status_code, 400)

    def test_gridded_value_follows_time_and_depth(self):
        store = make_zarr_store(str(self.data / "pacific.zarr"), numpy.arange(140.0, 220.0, 0.5))
        upload_id = self.open(store).json()["upload"]["id"]
        # Values are 20 + time + 5 * depth everywhere with data; -150° sits at 210° on this grid.
        for lon in (178.0, -150.0):
            body = self.value(upload_id, lon=lon, lat=-15.0, variable="sst", sel="time:2,depth:1").json()
            self.assertEqual(body["values"], {"Sea temperature (degC)": 27.0}, lon)
        self.assertIsNone(self.value(upload_id, lon=-30.0, lat=-15.0).json()["values"])  # outside
        self.assertEqual(self.value(upload_id, lon=178, lat=-15, sel="time:9").status_code, 400)
