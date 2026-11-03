from django.urls import path
from editor import views

urlpatterns = [
    path("", views.index, name="index"),
    path("channel/new/", views.channel_edit, name="channel_new"),
    path(
        "channel/<int:index>/edit/",
        views.channel_edit,
        name="channel_edit",
    ),
    path(
        "channel/<int:index>/delete/",
        views.channel_delete,
        name="channel_delete",
    ),
    path(
        "channel/<int:index>/duplicate/",
        views.channel_duplicate,
        name="channel_duplicate",
    ),
    path("undo/", views.undo, name="undo"),
    path("redo/", views.redo, name="redo"),
    path("zones/", views.zones, name="zones"),
    path("zones/<int:zid>/", views.zone_detail, name="zone_detail"),
    path("satellites/", views.sats_page, name="satellites"),
    path("satellites/new/", views.satellite_edit, name="satellite_new"),
    path(
        "satellites/<int:slot>/edit/",
        views.satellite_edit,
        name="satellite_edit",
    ),
    path(
        "satellites/norad-search",
        views.norad_search,
        name="norad_search",
    ),
    path(
        "satellites/transmitters",
        views.transmitters_search,
        name="transmitters_search",
    ),
    path("radio/read/", views.radio_read, name="radio_read"),
    path(
        "radio/read/confirm/",
        views.radio_read_confirm,
        name="radio_read_confirm",
    ),
    path("radio/write/", views.radio_write, name="radio_write"),
    path("radio/verify/", views.radio_verify, name="radio_verify"),
    path("api/job/", views.api_job, name="api_job"),
    path("api/job/cancel/", views.api_job_cancel, name="api_job_cancel"),
    path("csv/export/", views.csv_export, name="csv_export"),
    path("csv/import/", views.csv_import, name="csv_import"),
    path("backup/", views.backup, name="backup"),
    path("journal/", views.journal, name="journal"),
    path("help/", views.help_page, name="help"),
]
