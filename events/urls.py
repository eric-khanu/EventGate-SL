from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    # ---- public listing ----
    path("", views.home, name="home"),

    # ---- literal prefixes (MUST be above <slug:slug>/ ) ----
    path("create/",    views.event_create,        name="event_create"),
    path("dashboard/", views.organizer_dashboard, name="organizer_dashboard"),

    # ---- event-scoped routes ----
    path("<slug:slug>/",                  views.event_detail, name="event_detail"),
    path("<slug:slug>/edit/",             views.event_edit,   name="event_edit"),
    path("<slug:slug>/ticket-tiers/add/", views.tier_create,  name="tier_create"),
    path("<slug:slug>/ticket-tier/edit/", views.tier_edit,    name="tier_edit"),

    # Scanner management — MUST be above <slug:slug>/
    path("<slug:slug>/scanners/",              views.manage_scanners, name="manage_scanners"),
    path("<slug:slug>/scanners/add/",          views.add_scanner,     name="add_scanner"),
    path("<slug:slug>/scanners/<int:pk>/remove/", views.remove_scanner, name="remove_scanner"),
]