# accounts/middleware.py

from django.shortcuts import redirect
from django.urls import reverse, resolve, Resolver404

from .permissions import is_scanner_only


# URLs a scanner-only user may reach. Keep this list short and explicit.
ALLOWED_URL_NAMES = {
    # auth flow
    "accounts:login",
    "accounts:logout",

    # scanner
    "tickets:scanner",
    "tickets:scanner_scan",
    "tickets:scanner_stats",       # NEW — see below

    # static / media in dev
    "django.views.static.serve",   # only present when DEBUG; harmless otherwise

    # Django admin is NOT allowed for scanner-only users
}


class ScannerOnlyAccessMiddleware:
    """
    Block scanner-only users from every URL except ``ALLOWED_URL_NAMES``.

    Runs *after* ``AuthenticationMiddleware`` so ``request.user`` is
    resolved. Anonymous users are unaffected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not is_scanner_only(user):
            return self.get_response(request)

        # Resolve the URL name for this request.
        try:
            match = resolve(request.path_info)
        except Resolver404:
            # 404s fall through — Django handles them normally.
            return self.get_response(request)

        if match.view_name in ALLOWED_URL_NAMES:
            return self.get_response(request)

        # Also allow the scanner dashboard root "/" if you want the landing
        # page to be the scanner itself; otherwise redirect there.
        if request.path_info == "/":
            return redirect("tickets:scanner")

        return redirect("tickets:scanner")


# accounts/middleware.py — or a dedicated tickets middleware

from django.core.cache import cache

class ScanRateLimitMiddleware:
    """
    Token-bucket-ish per-scanner limit on the scan API.

    Not a substitute for auth — a forged request still hits the DB — but it
    blocks a compromised scanner from enumerating / brute-forcing tokens,
    and stops a runaway scanner app from spamming the endpoint.
    """
    LIMIT = 30          # scans
    WINDOW = 60         # seconds

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/tickets/scanner/validate/" and request.user.is_authenticated:
            key = f"scan-rl:{request.user.pk}"
            hits = cache.get(key, 0)
            if hits >= self.LIMIT:
                from django.http import JsonResponse
                return JsonResponse(
                    {"ok": False, "code": "RATE_LIMITED",
                     "message": "Too many scans. Try again in a minute."},
                    status=429,
                )
            cache.set(key, hits + 1, timeout=self.WINDOW)
        return self.get_response(request)