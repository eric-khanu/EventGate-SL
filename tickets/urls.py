from django.urls import path

from . import views

app_name = "tickets"

urlpatterns = [
    # ---- Buyer: checkout, own orders, own tickets ----
    path(
        "buy/<int:tier_id>/",
        views.checkout,
        name="checkout",
    ),
    path(
        "my-orders/",
        views.my_orders,
        name="my_orders",
    ),
    path(
        "view/<uuid:public_id>/",
        views.ticket_detail,
        name="ticket_detail",
    ),

    # ---- Scanner ----
    # Longest first, so future parameterised sub-routes can be added at the
    # bottom without silently shadowing these.
    path(
        "scanner/validate/",          # POST only (@require_POST)
        views.scanner_scan,
        name="scanner_scan",
    ),
    path(
        "scanner/stats/",
        views.scanner_stats,
        name="scanner_stats",
    ),
    path(
        "scanner/",                   # default landing after scanner login
        views.scanner,
        name="scanner",
    ),

    # ---- Organizer: sales report (reads Order, lives in tickets) ----
    path(
        "sales/",
        views.organizer_sales,
        name="organizer_sales",
    ),
]