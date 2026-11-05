import math
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.static import serve
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import datafiles, gridded, picker, raster, remote, tempfiles, tilesets
from .conversion import ConversionError, parse_crs
from .models import Upload

validate_http_url = URLValidator(schemes=["http", "https"])

STATIC_DIR = Path(__file__).parent / "static" / "viewer"


def index(request):
    # Changes whenever viewer.js or viewer.css do, so browsers don't run a stale copy.
    asset_version = max(int(p.stat().st_mtime) for p in STATIC_DIR.iterdir())
    return render(
        request,
        "viewer/index.html",
        {
            "cesium_ion_token": settings.CESIUM_ION_TOKEN,
            "asset_version": asset_version,
            "max_upload_size": settings.MAX_UPLOAD_SIZE,
            "temp_upload_hours": settings.TEMP_UPLOAD_HOURS,
            "is_local": _is_local(request),
        },
    )


@require_GET
def browse(request):
    try:
        return JsonResponse(datafiles.listing(request.GET.get("path")))
    except ConversionError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def pick(request):
    """Open the system file dialog and return the chosen path; for a browser on this machine."""
    if not _is_local(request):
        return JsonResponse(
            {"error": "The file dialog only works when the viewer runs on this computer.", "remote": True},
            status=403,
        )
    try:
        return JsonResponse({"path": picker.choose(folder=request.POST.get("folder") == "1")})
    except ConversionError as exc:
        return JsonResponse({"error": str(exc)}, status=500)


def _is_local(request):
    """True when the browser runs on the machine running the server."""
    return request.META.get("REMOTE_ADDR") in ("127.0.0.1", "::1")


@require_GET
def crs_definition(request):
    """A CRS as a proj4 string, for reprojecting in the browser (proj4js knows few CRSs)."""
    try:
        crs = parse_crs(request.GET.get("code", ""))
    except ConversionError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"proj4": crs.to_proj4()})


@require_POST
def upload_temp(request):
    """A file sent from the browser for a quick look; it and its layer expire."""
    Upload.purge_expired()
    file = request.FILES.get("file")
    if file is None:
        return JsonResponse({"error": "Send a file."}, status=400)
    if file.size > settings.MAX_UPLOAD_SIZE:
        limit_mb = settings.MAX_UPLOAD_SIZE // (1024 * 1024)
        return JsonResponse(
            {"error": f"File is larger than {limit_mb} MB. Raise MAX_UPLOAD_MB in .env to allow it."},
            status=400,
        )
    source_crs = request.POST.get("source_crs", "").strip()
    hint = request.POST.get("type", "").strip().lower() or None
    try:
        if source_crs:
            parse_crs(source_crs)
        path = tempfiles.save(file)
    except (ConversionError, OSError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    try:
        kind = Upload.kind_for_path(path, hint)
    except ConversionError as exc:
        tempfiles.remove(path)
        return JsonResponse({"error": str(exc)}, status=400)

    default_name = PurePosixPath(file.name).name.removesuffix(".zip").removesuffix(".zarr")
    default_name = default_name.removesuffix(".copc.laz")
    default_name = PurePosixPath(default_name).stem if "." in default_name else default_name
    upload = Upload.objects.create(
        name=(request.POST.get("name", "").strip() or default_name)[:255],
        kind=kind,
        source_path=str(path),
        source_crs=source_crs[:255],
        expires_at=timezone.now() + timedelta(hours=settings.TEMP_UPLOAD_HOURS),
    )
    upload.convert()
    if upload.status == Upload.Status.FAILED:
        error = upload.error
        upload.delete()
        return JsonResponse({"error": error}, status=400)
    return JsonResponse({"upload": upload.as_dict()}, status=201)


@require_GET
def upload_list(request):
    Upload.purge_expired()
    return JsonResponse({"uploads": [u.as_dict() for u in Upload.objects.all()]})


@require_POST
def upload_open(request):
    """Open a file or folder from a data folder, or a URL, as a layer. Nothing is copied."""
    path = request.POST.get("path", "").strip()
    url = request.POST.get("url", "").strip()
    if bool(path) == bool(url):
        return JsonResponse({"error": "Choose a file from the data folders, or enter a URL."}, status=400)

    source_crs = request.POST.get("source_crs", "").strip()
    hint = request.POST.get("type", "").strip().lower() or None
    try:
        if source_crs:
            parse_crs(source_crs)
        if path:
            source = datafiles.resolve(path)
            # A folder (not a Zarr store): list what in it can be plotted, to choose from.
            if source.is_dir() and not hint and datafiles.kind_for(source) is None:
                return JsonResponse({"folder": datafiles.listing(str(source))})
            fields = {"source_path": str(source), "kind": Upload.kind_for_path(source, hint)}
            default_name = source.name.removesuffix(".zarr").removesuffix(".copc.laz")
            default_name = PurePosixPath(default_name).stem if source.is_file() else default_name
        else:
            if remote.is_s3_folder(url) and not hint:
                return JsonResponse({"folder": remote.list_s3(url)})
            _validate_url(url)
            fields = {"source_url": url, "kind": Upload.kind_for_url(url, hint)}
            default_name = PurePosixPath(urlparse(url).path).stem or "Remote layer"
    except ConversionError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    # Opening the same source again reuses the layer, and a point cloud's 3D Tiles with it.
    upload = Upload.objects.filter(source_crs=source_crs, **fields).first()
    if upload is None:
        name = request.POST.get("name", "").strip() or default_name
        upload = Upload.objects.create(name=name[:255], source_crs=source_crs[:255], **fields)
    if upload.status != Upload.Status.READY:
        upload.convert()

    if upload.status == Upload.Status.FAILED:
        return JsonResponse({"error": upload.error, "upload": upload.as_dict()}, status=400)
    return JsonResponse({"upload": upload.as_dict()}, status=201)


def _validate_url(url):
    if url.startswith("s3://"):
        tilesets.parse_s3_url(url)
        return
    try:
        validate_http_url(url)
    except ValidationError:
        raise ConversionError("Enter a valid http(s) or s3:// URL.") from None


@require_http_methods(["DELETE"])
def upload_delete(request, pk):
    get_object_or_404(Upload, pk=pk).delete()
    return HttpResponse(status=204)


@require_GET
def upload_geojson(request, pk):
    upload = get_object_or_404(Upload, pk=pk, kind=Upload.Kind.VECTOR, status=Upload.Status.READY)
    try:
        collection, _crs = upload.vector_geojson()
    except (ConversionError, OSError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(collection, json_dumps_params={"default": str})


@require_GET
def upload_3dtiles(request, pk, path):
    upload = get_object_or_404(Upload, pk=pk, kind=Upload.Kind.POINTCLOUD, status=Upload.Status.READY)
    # serve() refuses paths that escape the tileset folder.
    return serve(request, path, document_root=upload.tiles_dir)


@require_GET
def upload_tile(request, pk, z, x, y):
    upload = get_object_or_404(
        Upload,
        pk=pk,
        kind__in=[Upload.Kind.RASTER, Upload.Kind.PMTILES, *Upload.GRIDDED],
        status=Upload.Status.READY,
    )

    if upload.kind == Upload.Kind.PMTILES:
        content, content_type = tilesets.render_tile(upload.raster_source, z, x, y)
        response = HttpResponse(content, content_type=content_type)
        response["Cache-Control"] = "public, max-age=3600"
        return response

    colormap = request.GET.get("colormap") or None
    if colormap and colormap not in raster.colormap_names():
        return JsonResponse({"error": f"Unknown colormap {colormap!r}."}, status=400)

    rescale = None
    if request.GET.get("rescale"):
        try:
            rescale = [float(v) for v in request.GET["rescale"].split(",")]
        except ValueError:
            rescale = None
        if not rescale or len(rescale) != 2 or not all(map(math.isfinite, rescale)):
            return JsonResponse({"error": "rescale must be 'min,max'."}, status=400)

    if upload.kind in Upload.GRIDDED:
        try:
            variable, sel = _zarr_selection(request, upload.raster_info)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        png = gridded.render_tile(
            upload.raster_source, upload.raster_info, z, x, y, variable, sel, colormap, rescale
        )
    else:
        png = raster.render_tile(
            upload.raster_source, upload.raster_info, z, x, y, colormap, rescale
        )
    response = HttpResponse(png, content_type="image/png")
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def upload_value(request, pk):
    """Data values at ?lon=&lat= for a raster or gridded layer (with its ?variable=&sel=)."""
    upload = get_object_or_404(
        Upload,
        pk=pk,
        kind__in=[Upload.Kind.RASTER, *Upload.GRIDDED],
        status=Upload.Status.READY,
    )
    try:
        lon = float(request.GET["lon"])
        lat = float(request.GET["lat"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "lon and lat are required numbers."}, status=400)
    if upload.kind == Upload.Kind.RASTER:
        return JsonResponse({"values": raster.value_at(upload.raster_source, lon, lat)})
    try:
        variable, sel = _zarr_selection(request, upload.raster_info)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    value = gridded.value_at(upload.raster_source, upload.raster_info, lon, lat, variable, sel)
    var_info = upload.raster_info["variables"][variable]
    label = var_info["long_name"] + (f" ({var_info['units']})" if var_info["units"] else "")
    return JsonResponse({"values": None if value is None else {label: value}})


def _terrain_upload(pk):
    return get_object_or_404(
        Upload,
        pk=pk,
        kind=Upload.Kind.PMTILES,
        status=Upload.Status.READY,
        raster_info__tile_type="terrain",
    )


@require_GET
def terrain_layer(request, pk):
    upload = _terrain_upload(pk)
    return JsonResponse(tilesets.terrain_layer(upload.raster_source))


@require_GET
def terrain_tile(request, pk, z, x, y):
    upload = _terrain_upload(pk)
    data = tilesets.terrain_tile(upload.raster_source, z, x, y)
    if not data:
        return HttpResponse(status=404)
    response = HttpResponse(data, content_type="application/vnd.quantized-mesh")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def _zarr_selection(request, info):
    """Parse ?variable=sst&sel=time:3,depth:0 into a variable name and {dim: index}."""
    variable = request.GET.get("variable") or info["default_variable"]
    if variable not in info["variables"]:
        raise ValueError(f"Unknown variable {variable!r}.")
    sizes = {d["name"]: len(d["values"]) for d in info["variables"][variable]["dims"]}
    sel = {}
    for part in filter(None, request.GET.get("sel", "").split(",")):
        dim, _, index = part.partition(":")
        if dim not in sizes or not index.isdigit() or int(index) >= sizes[dim]:
            raise ValueError(f"Invalid selection {part!r}.")
        sel[dim] = int(index)
    return variable, sel
