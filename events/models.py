import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


# ---------------------------------------------------------------------------
# Upload validators
# ---------------------------------------------------------------------------
MAX_COVER_BYTES = 5 * 1024 * 1024        # 5 MB
MAX_VIDEO_BYTES = 200 * 1024 * 1024      # 200 MB

COVER_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm"}


def validate_cover_image(f):
    """Reject oversized or obviously wrong-type cover uploads."""
    if f.size > MAX_COVER_BYTES:
        raise ValidationError("Cover image must be 5 MB or smaller.")
    ct = getattr(f, "content_type", "")
    if ct and ct not in COVER_CONTENT_TYPES:
        raise ValidationError(
            f"Unsupported cover type {ct!r}. Use JPEG, PNG, or WebP."
        )


def validate_video_file(f):
    """Reject oversized or obviously wrong-type video uploads."""
    if f.size > MAX_VIDEO_BYTES:
        raise ValidationError("Video file must be 200 MB or smaller.")
    ct = getattr(f, "content_type", "")
    if ct and ct not in VIDEO_CONTENT_TYPES:
        raise ValidationError(
            f"Unsupported video type {ct!r}. Use MP4 or WebM."
        )


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils.text import slugify


class Event(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending review"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class Category(models.TextChoices):
        MUSIC = "music festival", "Music festival"
        SPORTS = "sports", "Sports"
        TECH = "tech", "Tech"
        ARTS = "arts", "Arts & Culture"
        BUSINESS = "business", "Business"
        COMMUNITY = "community", "Community"
        OTHER = "other", "Other"

    class Currency(models.TextChoices):
        SLE = "SLE", "SLE"
        USD = "USD", "USD"

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="events",
    )
    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=200, unique=True, editable=False, blank=True)

    summary = models.CharField(max_length=300)
    description = models.TextField()

    category = models.CharField(
        max_length=80, choices=Category.choices, default=Category.OTHER,
    )
    venue = models.CharField(max_length=220)
    district = models.CharField(max_length=100, help_text="District in Sierra Leone")
    region = models.CharField(max_length=100, default="Western Area")

    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    cover_image = models.ImageField(
        upload_to="event-covers/%Y/%m/",
        blank=True,
        validators=[validate_cover_image],
    )

    contact_phone = models.CharField(max_length=30)
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.SLE,
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["status", "starts_at"]),
            models.Index(fields=["district", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")),
                name="event_ends_after_starts",
            ),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("events:event_detail", kwargs={"slug": self.slug})

    # ------------------------------------------------------------------
    # Convenience helpers (used by admin + actions)
    # ------------------------------------------------------------------
    def active_tiers(self):
        return self.ticket_tiers.filter(is_active=True)

    def has_active_tier(self) -> bool:
        return self.active_tiers().exists()

    # ------------------------------------------------------------------
    # Saving / slug
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        """
        Auto-generate a unique slug from ``title`` on first save.

        Uses an IntegrityError-retry loop so concurrent creates of the same
        title don't both claim ``my-event`` (the DB unique index on ``slug``
        is the source of truth; we retry with a ``-2``, ``-3``, ... suffix).
        Only retries on slug collisions — any other IntegrityError is
        re-raised so unrelated bugs aren't masked.
        """
        if self.slug:
            return super().save(*args, **kwargs)

        base = (slugify(self.title) or "event")[:170]
        candidate, suffix = base, 2

        for _ in range(50):
            self.slug = candidate
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if "slug" not in str(exc).lower():
                    raise
                candidate = f"{base}-{suffix}"
                suffix += 1

        raise IntegrityError(
            f"Could not allocate a unique slug for {self.title!r} after 50 tries."
        )
    
# ---------------------------------------------------------------------------
# EventScanner
# ---------------------------------------------------------------------------
class EventScanner(models.Model):
    """A user permitted to scan tickets for a specific event."""

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="scanners",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scanner_assignments",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("event", "user")]
        indexes = [models.Index(fields=["user", "event"])]

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="eventscanner_unique_event_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} → {self.event}"


# ---------------------------------------------------------------------------
# EventVideo
# ---------------------------------------------------------------------------
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_VIMEO_ID = re.compile(r"^\d{5,15}$")


class EventVideo(models.Model):
    """A video attached to an event (YouTube/Vimeo link or uploaded file)."""

    class Source(models.TextChoices):
        YOUTUBE = "youtube", "YouTube"
        VIMEO = "vimeo", "Vimeo"
        UPLOAD = "upload", "Uploaded file"
        OTHER = "other", "Other"

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="videos",
    )
    title = models.CharField(max_length=150, blank=True)
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.YOUTUBE,
    )
    # Use one of the two below depending on ``source``; both may be blank.
    url = models.URLField(blank=True, help_text="YouTube/Vimeo/other embed URL")
    file = models.FileField(
        upload_to="event-videos/%Y/%m/",
        blank=True,
        validators=[validate_video_file],
        help_text="Uploaded video (ignored if ``url`` is set)",
    )
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [models.Index(fields=["event", "order"])]
        # NOTE: the "at least one source" rule lives in ``clean()`` only —
        # Django's CheckConstraint can't express ``url=""`` vs NULL the way
        # the original tried to, and a faulty constraint silently allows
        # bad rows through. Enforcing it at the form layer is reliable.

    def __str__(self):
        return self.title or f"{self.event.title} — video {self.pk}"

    def clean(self):
        if not self.url and not self.file:
            raise ValidationError("Provide either a URL or an uploaded file.")
        if self.url and self.file:
            raise ValidationError("Provide only a URL *or* a file, not both.")

    @property
    def embed_url(self):
        """
        Normalise common YouTube/Vimeo watch URLs into embeddable URLs.

        The extracted video ID is validated against a strict pattern; if it
        doesn't look like a real ID, an empty string is returned rather than
        the raw (possibly attacker-influenced) URL — a template that does
        ``<iframe src="{{ video.embed_url }}">`` must never end up with
        quotes or angle brackets inside ``src``.
        """
        if not self.url:
            return ""
        url = self.url

        if self.source == self.Source.YOUTUBE:
            if "youtu.be/" in url:
                vid = url.rsplit("youtu.be/", 1)[1].split("?")[0]
                return (
                    f"https://www.youtube.com/embed/{vid}"
                    if _YOUTUBE_ID.match(vid) else ""
                )
            if "watch?v=" in url:
                vid = url.rsplit("v=", 1)[1].split("&")[0]
                return (
                    f"https://www.youtube.com/embed/{vid}"
                    if _YOUTUBE_ID.match(vid) else ""
                )

        if self.source == self.Source.VIMEO:
            tail = url.rstrip("/").split("/")[-1]
            if _VIMEO_ID.match(tail):
                return f"https://player.vimeo.com/video/{tail}"
            return ""

        # ``OTHER`` and uploaded files: return the raw URL; the template is
        # expected to render it inside a link, not an iframe ``src``.
        return url


# ---------------------------------------------------------------------------
# TicketTier
# ---------------------------------------------------------------------------
class TicketTier(models.Model):
    """
    A priced tier of tickets for an event.

    Early-bird discount applies automatically between ``discount_starts_at``
    (inclusive) and ``discount_ends_at`` (exclusive). If either bound is null,
    the window is treated as open-ended on that side.

    Business rule: at most **one active tier per event** — enforced by the
    ``tier_one_active_per_event`` partial unique constraint. Inactive tiers
    may coexist alongside it as purchase history.
    """

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="ticket_tiers",
    )
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=250, blank=True)

    # ---- Pricing & stock ----
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    quantity = models.PositiveIntegerField(
        help_text="Total tickets allocated for this tier.",
    )
    sold = models.PositiveIntegerField(
        default=0,
        editable=False,
        help_text="Incremented by checkout; not editable in admin.",
    )

    # ---- Early-bird discount ----
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
        help_text="Discount applied during the early-bird window, e.g. 15 for 15%.",
    )
    discount_starts_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the early-bird discount becomes active.",
    )
    discount_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the early-bird discount expires.",
    )

    # ---- General sale window ----
    sale_starts = models.DateTimeField(null=True, blank=True)
    sale_ends = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["event_id", "price"]
        verbose_name = "ticket tier"
        verbose_name_plural = "ticket tiers"

        constraints = [
            # ---- Uniqueness ----
            models.UniqueConstraint(
                fields=["event", "name"],
                name="tier_event_name_unique",
            ),
            # At most one *active* tier per event.
            models.UniqueConstraint(
                fields=["event"],
                condition=Q(is_active=True),
                name="tier_one_active_per_event",
            ),

            # ---- Price / stock sanity ----
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="tier_price_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(sold__gte=0),
                name="tier_sold_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(sold__lte=F("quantity")),
                name="tier_sold_not_over_quantity",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=0),
                name="tier_quantity_nonnegative",
            ),

            # ---- Discount ----
            models.CheckConstraint(
                condition=Q(discount_percent__gte=0)
                & Q(discount_percent__lte=100),
                name="tier_discount_percent_range",
            ),
            # Both bounds set → end must be strictly after start.
            # Half-open windows (only one bound set) are allowed by design.
            models.CheckConstraint(
                condition=(
                    Q(discount_ends_at__isnull=True)
                    | Q(discount_starts_at__isnull=True)
                    | Q(discount_ends_at__gt=F("discount_starts_at"))
                ),
                name="tier_discount_window_valid",
            ),

            # ---- Sale window ----
            models.CheckConstraint(
                condition=(
                    Q(sale_ends__isnull=True)
                    | Q(sale_starts__isnull=True)
                    | Q(sale_ends__gt=F("sale_starts"))
                ),
                name="tier_sale_window_valid",
            ),
        ]

    def __str__(self):
        return f"{self.event.title} — {self.name}"

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    @property
    def remaining(self):
        """Tickets still available for purchase (ignores time windows)."""
        return max(self.quantity - self.sold, 0)

    @property
    def is_sold_out(self):
        return self.sold >= self.quantity

    @property
    def sell_through_pct(self):
        """0–100 integer, for progress bars. Returns 0 when quantity=0."""
        if not self.quantity:
            return 0
        return int((self.sold / self.quantity) * 100)

    @property
    def available(self):
        """Backwards-compatible alias for ``remaining``."""
        return self.remaining

    # ------------------------------------------------------------------
    # Time windows
    # ------------------------------------------------------------------
    def _within(self, now, start, end):
        if start and now < start:
            return False
        if end and now >= end:
            return False
        return True

    @property
    def discount_active(self):
        """True if now falls inside the early-bird window and a discount is set."""
        if self.discount_percent <= 0:
            return False
        return self._within(
            timezone.now(),
            self.discount_starts_at,
            self.discount_ends_at,
        )

    @property
    def on_sale(self):
        """True if the tier can be purchased *right now*."""
        if not self.is_active or self.remaining <= 0:
            return False
        return self._within(timezone.now(), self.sale_starts, self.sale_ends)

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------
    @property
    def current_price(self):
        """Effective price right now — applies the early-bird discount if live."""
        if self.discount_active:
            multiplier = Decimal("1") - (
                self.discount_percent / Decimal("100")
            )
            return (self.price * multiplier).quantize(Decimal("0.01"))
        return self.price

    def price_at(self, moment):
        """Effective price at an arbitrary ``moment`` (useful for receipts)."""
        if self.discount_percent <= 0:
            return self.price
        if not self._within(
            moment, self.discount_starts_at, self.discount_ends_at
        ):
            return self.price
        multiplier = Decimal("1") - (self.discount_percent / Decimal("100"))
        return (self.price * multiplier).quantize(Decimal("0.01"))

    # ------------------------------------------------------------------
    # Oversell-safe increment
    # ------------------------------------------------------------------
    def reserve(self, quantity=1):
        """
        Atomically reserve ``quantity`` tickets.

        Returns ``True`` on success, ``False`` if not enough stock remains.
        Caller should wrap in ``transaction.atomic()`` when reserving
        alongside other writes (e.g. creating an Order).
        """
        updated = TicketTier.objects.filter(
            pk=self.pk,
            sold__lte=F("quantity") - quantity,
        ).update(sold=F("sold") + quantity)
        if updated:
            self.refresh_from_db(fields=["sold"])
        return bool(updated)

