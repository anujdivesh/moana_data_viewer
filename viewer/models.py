import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.urls import reverse

from . import datafiles, gridded, pointcloud, raster, remote, tempfiles, tilesets
from .conversion import ConversionError, path_to_geojson

logger = logging.getLogger(__name__)


class Upload(models.Model):
    """A layer on the globe: a file or folder read in place, or a remote URL."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    class Kind(models.TextChoices):
        VECTOR = "vector", "Vector"
        RASTER = "raster", "Raster"
        ZARR = "zarr", "Zarr"
        NETCDF = "netcdf", "NetCDF"
        POINTCLOUD = "pointcloud", "Point cloud"
        PMTILES = "pmtiles", "PMTiles"

    # Read with xarray and drawn the same way.
    GRIDDED = (Kind.ZARR, Kind.NETCDF)

    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=10, choices=Kind, default=Kind.VECTOR)
    source_path = models.CharField(
        max_length=1000,
        blank=True,
        help_text="File or folder on the server, read in place.",
    )
    # Not a URLField, so s3:// URLs are accepted.
    source_url = models.CharField(
        max_length=1000,
        blank=True,
        help_text="A remote GeoTIFF or PMTiles archive (http/https or s3://bucket/key), read in place.",
    )
    raster_info = models.JSONField(default=dict, blank=True)
    source_crs = models.CharField(
        max_length=255,
        blank=True,
        help_text="Overrides the file's CRS, e.g. EPSG:32760. Needed when a shapefile has no .prj.",
    )
    detected_crs = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True)
    feature_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="For files sent from a browser: when they are deleted."
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @classmethod
    def purge_expired(cls):
        """Delete temporary layers (and their files) whose time is up."""
        for upload in cls.objects.filter(expires_at__lte=timezone.now()):
            upload.delete()

    @staticmethod
    def kind_for_path(path, hint=None):
        kind = hint or datafiles.kind_for(path)
        if kind is None:
            raise ConversionError(
                f"{Path(path).name} is not a file type the viewer can show. If it is, add a type, "
                f"e.g. &type=netcdf (one of {', '.join(remote.KINDS)})."
            )
        if kind not in remote.KINDS:
            raise ConversionError(f"Unknown type {kind!r}; use one of {', '.join(remote.KINDS)}.")
        return Upload.Kind(kind)

    @staticmethod
    def kind_for_url(url, hint=None):
        return Upload.Kind(remote.kind_for_url(url, hint))

    def vector_geojson(self):
        """(FeatureCollection in WGS84, source CRS name), read from the source each time."""
        crs = self.source_crs or None
        if self.source_url:
            return remote.vector_to_geojson(self.source_url, crs)
        return path_to_geojson(self.source_path, crs)

    @property
    def raster_source(self):
        """Path or URL that tiles are read from."""
        if self.kind == self.Kind.RASTER and self.source_crs and self.source_path:
            return raster.with_crs(self.source_path, self.source_crs)
        return self.source_path or self.source_url

    @property
    def tiles_dir(self):
        """3D Tiles tileset made from a point cloud."""
        return Path(settings.CACHE_DIR) / "3dtiles" / str(self.pk)

    def convert(self):
        """Prepare this layer for display and record the outcome on it."""
        try:
            if self.source_path and not Path(self.source_path).exists():
                raise ConversionError(f"{self.source_path} no longer exists.")
            if self.kind == self.Kind.RASTER:
                self._convert_raster()
            elif self.kind in self.GRIDDED:
                self._convert_gridded()
            elif self.kind == self.Kind.POINTCLOUD:
                self._convert_pointcloud()
            elif self.kind == self.Kind.PMTILES:
                self._convert_pmtiles()
            else:
                self._convert_vector()
        except ConversionError as exc:
            self.status = self.Status.FAILED
            self.error = str(exc)
            self.save()
            return
        except Exception as exc:
            # Real-world files hit cases the readers don't anticipate; record them on the
            # layer (with the traceback in the server log) instead of failing the request.
            logger.exception("Opening layer %s (%s) failed", self.pk, self.name)
            self.status = self.Status.FAILED
            self.error = f"Unexpected error: {type(exc).__name__}: {exc}"
            self.save()
            return
        self.status = self.Status.READY
        self.error = ""
        self.save()

    def _convert_vector(self):
        # Read once to check it and count features; it is served from the source on request.
        collection, crs = self.vector_geojson()
        self.feature_count = len(collection["features"])
        self.detected_crs = crs

    def _convert_raster(self):
        if self.source_url and self.source_crs:
            raise ConversionError(
                "A source CRS can't be applied to a remote GeoTIFF; set it in the file instead."
            )
        self.raster_info = raster.describe(self.raster_source)
        self.detected_crs = self.raster_info["crs"]

    def _convert_gridded(self):
        gridded.open_store.cache_clear()
        self.raster_info = gridded.describe(self.raster_source, self.kind)
        self.detected_crs = self.raster_info["crs"]

    def _convert_pointcloud(self):
        if not self.source_path:
            raise ConversionError(
                "Point clouds must be opened from a file on this machine; download it first."
            )
        self.raster_info = pointcloud.to_3dtiles(
            self.source_path, self.tiles_dir, self.source_crs or None
        )
        self.detected_crs = self.raster_info["crs"]

    def _convert_pmtiles(self):
        if self.source_crs:
            raise ConversionError("PMTiles are always in Web Mercator; leave the source CRS empty.")
        tilesets.clear_cache()
        self.raster_info = tilesets.describe(self.raster_source)
        self.detected_crs = self.raster_info["crs"]

    def delete(self, *args, **kwargs):
        # Only this app's cache and files sent for a quick look; sources are never touched.
        shutil.rmtree(self.tiles_dir, ignore_errors=True)
        if self.source_path:
            tempfiles.remove(self.source_path)
        return super().delete(*args, **kwargs)

    def as_dict(self):
        data = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "source": self.source_path or self.source_url,
            "source_crs": self.source_crs,
            "detected_crs": self.detected_crs,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
        if self.kind == self.Kind.POINTCLOUD:
            data["raster_info"] = self.raster_info
            if self.status == self.Status.READY:
                data["tileset_url"] = reverse("viewer:upload-3dtiles", args=[self.id, "tileset.json"])
        elif self.kind == self.Kind.PMTILES and self.raster_info.get("tile_type") == "terrain":
            data["raster_info"] = self.raster_info
            data["terrain_url"] = reverse("viewer:terrain-layer", args=[self.id])
        elif self.kind in (self.Kind.RASTER, self.Kind.PMTILES, *self.GRIDDED):
            data["raster_info"] = self.raster_info
            data["tile_url"] = reverse(
                "viewer:upload-tile", args=[self.id, 0, 0, 0]
            ).replace("/0/0/0.png", "/{z}/{x}/{y}.png")
        else:
            data["feature_count"] = self.feature_count
            data["geojson_url"] = reverse("viewer:upload-geojson", args=[self.id])
        return data
