from django.urls import path

from . import views

app_name = "viewer"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/browse/", views.browse, name="browse"),
    path("api/pick/", views.pick, name="pick"),
    path("api/crs/", views.crs_definition, name="crs"),
    path("api/uploads/temp/", views.upload_temp, name="upload-temp"),
    path("api/uploads/", views.upload_list, name="upload-list"),
    path("api/uploads/open/", views.upload_open, name="upload-open"),
    path("api/uploads/<int:pk>/", views.upload_delete, name="upload-delete"),
    path("api/uploads/<int:pk>/geojson/", views.upload_geojson, name="upload-geojson"),
    path("api/uploads/<int:pk>/3dtiles/<path:path>", views.upload_3dtiles, name="upload-3dtiles"),
    path(
        "api/uploads/<int:pk>/tiles/<int:z>/<int:x>/<int:y>.png",
        views.upload_tile,
        name="upload-tile",
    ),
    path("api/uploads/<int:pk>/value/", views.upload_value, name="upload-value"),
    path("api/uploads/<int:pk>/terrain/layer.json", views.terrain_layer, name="terrain-layer"),
    path(
        "api/uploads/<int:pk>/terrain/<int:z>/<int:x>/<int:y>.terrain",
        views.terrain_tile,
        name="terrain-tile",
    ),
]
