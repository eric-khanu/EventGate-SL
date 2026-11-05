"""URL configuration for the orders app.

Final URL shapes (with ``path("orders/", include("orders.urls"))``):

    /orders/<pk>/payment-proof/                  → order_payment_proof (POST/GET)
    /orders/<pk>/payment-proof/view/             → order_payment_proof_view (GET inline)
    /orders/<reference>/payment-proof/download/  → download_payment_proof (GET attachment)
    /orders/verify/<slug>/                       → verify_orders
    /orders/verify/<slug>/<reference>/approve/   → approve_order (POST)
    /orders/verify/<slug>/<reference>/reject/    → reject_order (POST)
"""

from django.urls import path
from . import views

app_name = "orders"

urlpatterns = [
    # ---- Payment proof ----
    path(
        "<int:pk>/payment-proof/",
        views.order_payment_proof,
        name="order_payment_proof",
    ),
    path(
        "<int:pk>/payment-proof/view/",
        views.order_payment_proof_view,
        name="order_payment_proof_view",
    ),
    path(
        "<str:reference>/payment-proof/download/",
        views.download_payment_proof,
        name="download_payment_proof",
    ),

    # ---- Organizer / staff: verification queue ----
    path(
        "verify/<slug:slug>/",
        views.verify_orders,
        name="verify_orders",
    ),
    path(
        "verify/<slug:slug>/<str:reference>/approve/",
        views.approve_order,
        name="approve_order",
    ),
    path(
        "verify/<slug:slug>/<str:reference>/reject/",
        views.reject_order,
        name="reject_order",
    ),
]