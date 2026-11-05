"""URL configuration for the tickets app.

Routes here mirror the views defined in ``tickets/views.py`` only. Order
verification (verify / approve / reject) lives in ``orders.urls`` and is
*not* routed here — if a view is added to ``views.py``, add its route here;
if it moves to ``orders``, delete its route here.

``healthz`` is defined in ``tickets/views.py`` for proximity to the code it
probes, but is **routed at the project level** (``/healthz/``), not here —
load-balancer probes don't belong under a feature-app prefix.

Ordering rule
-------------
Within a group, put the more specific path first. Django matches in
declaration order, and although ``path()`` requires an exact match today
(so ``"scanner/"`` does not swallow ``"scanner/stats/"``), that guarantee
only holds while no sibling has a parameterised segment. The moment
someone adds ``"scanner/<str:subpage>/"`` at the bottom, every path above
it that starts with ``"scanner/"`` stops matching the literal subpaths
they used to. Keeping the longest / most specific path first makes the
route set correct by *construction*, not by luck.
"""

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