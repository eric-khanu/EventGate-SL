from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from events.views import home
from tickets.views import healthz

admin.site.site_header = "EventGate administration"
admin.site.site_title = "EventGate"
admin.site.index_title = "Operations"

urlpatterns = [
    path("admin/", admin.site.urls),

    # Health check for load balancers / uptime monitors.
    path("healthz/", healthz, name="healthz"),

    # Public homepage is served by the events app, but lives at "/".
    path("", home, name="home"),

    # Application namespaces.
    path("accounts/",     include("accounts.urls")),
    path("events/",       include("events.urls")),
    path("orders/",       include("orders.urls")),
    path("tickets/",      include("tickets.urls")),
    path("advertising/",  include("advertisements.urls")),
]

# Serve user-uploaded media in development only. In production, the web
# server (nginx) or object storage (S3 / R2) serves MEDIA_URL.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)