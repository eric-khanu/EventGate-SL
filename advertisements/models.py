"""Models for advertisement plans, subscriptions, and campaigns."""

import mimetypes
from decimal import Decimal
from pathlib import Path

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator, MinValueValidator, URLValidator,
)
from django.db import models, transaction
from django.utils import timezone


# ---------------------------------------------------------------------------
# Shared choices
# ---------------------------------------------------------------------------
class Currency(models.TextChoices):
    SLE = "SLE", "SLE"
    USD = "USD", "USD"


# ---------------------------------------------------------------------------
# Upload validation (ad proofs, images, videos)
# ---------------------------------------------------------------------------
MAX_AD_PROOF_BYTES = 5 * 1024 * 1024
MAX_AD_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AD_VIDEO_BYTES = 25 * 1024 * 1024

ALLOWED_AD_PROOF_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
ALLOWED_AD_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_AD_VIDEO_TYPES = {"video/mp4", "video/webm"}


def _detect_content_type(uploaded_file) -> str:
    """Return MIME from magic bytes. Requires ``python-magic``."""
    try:
        import magic
    except ImportError as exc:
        raise ValidationError(
            "Server is missing the 'python-magic' dependency — uploads are "
            "disabled. Ask the administrator to install it."
        ) from exc
    uploaded_file.seek(0)
    mime = magic.from_buffer(uploaded_file.read(2048), mime=True)
    uploaded_file.seek(0)
    return mime


def _validate_upload(uploaded_file, *, max_bytes, allowed_types, label):
    if uploaded_file.size > max_bytes:
        raise ValidationError(
            f"{label} is too large ({uploaded_file.size} bytes); max is "
            f"{max_bytes} bytes."
        )
    ct = _detect_content_type(uploaded_file)
    if ct not in allowed_types:
        raise ValidationError(
            f"Unsupported {label.lower()} type: {ct}. "
            f"Allowed: {sorted(allowed_types)}"
        )
    return ct


def ad_proof_upload_to(instance, filename):
    """Always store without extension; type comes from ``payment_proof_content_type``."""
    now = timezone.now()
    ref = getattr(instance, "pk", None) or "pending"
    return f"ad-payment-proofs/{now:%Y/%m}/{ref}/proof"


def ad_image_upload_to(instance, filename):
    now = timezone.now()
    return f"advertisements/images/{now:%Y/%m}/{Path(filename).stem[:40]}.webp"


def ad_video_upload_to(instance, filename):
    now = timezone.now()
    ext = Path(filename).suffix.lower() or ".mp4"
    if ext not in {".mp4", ".webm"}:
        ext = ".mp4"
    return f"advertisements/videos/{now:%Y/%m}/{Path(filename).stem[:40]}{ext}"


# ---------------------------------------------------------------------------
# Ad plan
# ---------------------------------------------------------------------------
class AdPlan(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    duration_months = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(36)],
    )
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.SLE,
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "duration_months", "price"]

    def __str__(self):
        return f"{self.name} ({self.duration_months} months)"


# ---------------------------------------------------------------------------
# Ad subscription
# ---------------------------------------------------------------------------
class AdSubscription(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Payment submitted"
        ACTIVE = "active", "Active"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class PaymentMethod(models.TextChoices):
        ORANGE_MONEY = "orange_money", "Orange Money"
        AFRIMONEY = "afrimoney", "Afrimoney"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        OTHER = "other", "Other"

    advertiser = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ad_subscriptions",
    )
    plan = models.ForeignKey(
        AdPlan, on_delete=models.PROTECT, related_name="subscriptions",
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(
        max_length=3, choices=Currency.choices, default=Currency.SLE,
    )
    payment_method = models.CharField(max_length=40, choices=PaymentMethod.choices)
    payment_reference = models.CharField(max_length=160)
    payment_notes = models.TextField(blank=True)
    payment_proof = models.FileField(
        upload_to=ad_proof_upload_to, blank=True,
    )
    payment_proof_content_type = models.CharField(max_length=100, blank=True)
    payment_proof_uploaded_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="verified_ad_subscriptions",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["advertiser", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="ad_subscription_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="active")
                    | models.Q(
                        starts_at__isnull=False,
                        expires_at__isnull=False,
                    )
                ),
                name="ad_subscription_active_has_window",
            ),
            models.CheckConstraint(
                condition=models.Q(payment_proof_content_type__in=[
                    "", "image/jpeg", "image/png", "image/webp", "application/pdf",
                ]),
                name="ad_subscription_proof_content_type_whitelist",
            ),
        ]

    def __str__(self):
        return f"{self.advertiser} — {self.plan}"

    # ------------------------------------------------------------------
    # Payment proof
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_payment_proof(uploaded_file):
        return _validate_upload(
            uploaded_file,
            max_bytes=MAX_AD_PROOF_BYTES,
            allowed_types=ALLOWED_AD_PROOF_TYPES,
            label="Payment proof",
        )

    def attach_payment_proof(self, uploaded_file):
        ct = self._validate_payment_proof(uploaded_file)
        old = self.payment_proof if self.payment_proof else None

        with transaction.atomic():
            AdSubscription.objects.filter(pk=self.pk).update(
                payment_proof=uploaded_file,
                payment_proof_content_type=ct,
                payment_proof_uploaded_at=timezone.now(),
            )
        self.refresh_from_db(fields=[
            "payment_proof", "payment_proof_content_type",
            "payment_proof_uploaded_at",
        ])
        if old and old.name and old.name != self.payment_proof.name:
            try:
                old.delete(save=False)
            except Exception:  # noqa: BLE001
                pass

    def payment_proof_filename(self):
        ext = mimetypes.guess_extension(
            self.payment_proof_content_type or ""
        ) or ".bin"
        return f"ad_proof_{self.pk or 'new'}{ext}"

    # ------------------------------------------------------------------
    # State transitions (locked + auto-expire siblings)
    # ------------------------------------------------------------------
    def activate(self, verifier):
        with transaction.atomic():
            locked = (
                AdSubscription.objects
                .select_for_update()
                .select_related("plan")
                .get(pk=self.pk)
            )
            if locked.status not in (self.Status.PENDING, self.Status.ACTIVE):
                raise ValueError(f"Cannot activate a {locked.status} subscription.")

            now = timezone.now()
            locked.status = self.Status.ACTIVE
            locked.verified_by = verifier
            locked.verified_at = now
            locked.starts_at = now
            locked.expires_at = now + relativedelta(
                months=locked.plan.duration_months,
            )
            locked.save(update_fields=[
                "status", "verified_by", "verified_at",
                "starts_at", "expires_at",
            ])

            # Only one ACTIVE subscription per advertiser.
            AdSubscription.objects.filter(
                advertiser_id=locked.advertiser_id,
                status=self.Status.ACTIVE,
            ).exclude(pk=locked.pk).update(status=self.Status.EXPIRED)

            self.status = locked.status
            self.starts_at = locked.starts_at
            self.expires_at = locked.expires_at
            self.verified_at = locked.verified_at
            self.verified_by_id = locked.verified_by_id

    def reject(self, verifier):
        with transaction.atomic():
            locked = AdSubscription.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise ValueError("Only pending subscriptions can be rejected.")
            locked.status = self.Status.REJECTED
            locked.verified_by = verifier
            locked.verified_at = timezone.now()
            locked.save(update_fields=["status", "verified_by", "verified_at"])
            self.status = locked.status
            self.verified_at = locked.verified_at
            self.verified_by_id = locked.verified_by_id

    def cancel(self):
        with transaction.atomic():
            locked = AdSubscription.objects.select_for_update().get(pk=self.pk)
            if locked.status not in (self.Status.PENDING, self.Status.ACTIVE):
                raise ValueError(
                    "Only pending or active subscriptions can be cancelled."
                )
            locked.status = self.Status.CANCELLED
            locked.save(update_fields=["status"])
            self.status = locked.status

    def expire(self):
        with transaction.atomic():
            locked = AdSubscription.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.ACTIVE:
                return
            locked.status = self.Status.EXPIRED
            locked.save(update_fields=["status"])
            self.status = locked.status

    @property
    def is_current(self):
        return (
            self.status == self.Status.ACTIVE
            and self.expires_at is not None
            and self.expires_at > timezone.now()
        )


# ---------------------------------------------------------------------------
# Ad campaign
# ---------------------------------------------------------------------------
class AdCampaign(models.Model):
    class Placement(models.TextChoices):
        HERO = "hero", "Homepage featured banner"
        BANNER = "banner", "Banner"
        SIDEBAR = "sidebar", "Sidebar"
        SPOTLIGHT = "spotlight", "Company spotlight"
        IN_FEED = "in_feed", "Event feed"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Awaiting review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAUSED = "paused", "Paused"

    advertiser = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ad_campaigns",
    )
    subscription = models.ForeignKey(
        AdSubscription, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="campaigns",
    )
    company_name = models.CharField(max_length=160)
    title = models.CharField(max_length=160)
    caption = models.CharField(max_length=280, blank=True)
    image = models.ImageField(
        upload_to=ad_image_upload_to, blank=True,
    )
    video = models.FileField(
        upload_to=ad_video_upload_to, blank=True,
        help_text="MP4/WebM video, maximum 25 MB.",
    )
    target_url = models.URLField(
        max_length=500,
        validators=[URLValidator(schemes=["http", "https"])],
    )
    placement = models.CharField(
        max_length=12, choices=Placement.choices,
        default=Placement.BANNER, db_index=True,
    )
    status = models.CharField(
        max_length=12, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    impressions = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "placement", "ends_at"]),
            models.Index(fields=["advertiser", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(starts_at__isnull=True)
                    | models.Q(ends_at__gt=models.F("starts_at"))
                ),
                name="ad_campaign_window_valid",
            ),
        ]

    def __str__(self):
        return f"{self.company_name}: {self.title}"

    # ------------------------------------------------------------------
    # Creative validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_image(uploaded_file):
        return _validate_upload(
            uploaded_file,
            max_bytes=MAX_AD_IMAGE_BYTES,
            allowed_types=ALLOWED_AD_IMAGE_TYPES,
            label="Image",
        )

    @staticmethod
    def _validate_video(uploaded_file):
        return _validate_upload(
            uploaded_file,
            max_bytes=MAX_AD_VIDEO_BYTES,
            allowed_types=ALLOWED_AD_VIDEO_TYPES,
            label="Video",
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def approve(self, verifier=None):
        if self.status not in (self.Status.DRAFT, self.Status.PENDING):
            raise ValueError(f"Cannot approve a {self.status} campaign.")
        if not self.subscription or not self.subscription.is_current:
            raise ValueError("Campaign has no active subscription.")

        now = timezone.now()
        self.status = self.Status.APPROVED
        self.starts_at = self.starts_at or now
        # Campaign cannot outlive the subscription.
        if self.ends_at is None or self.ends_at > self.subscription.expires_at:
            self.ends_at = self.subscription.expires_at
        self.save(update_fields=["status", "starts_at", "ends_at", "updated_at"])

    def reject(self, verifier=None):
        if self.status not in (self.Status.DRAFT, self.Status.PENDING):
            raise ValueError(f"Cannot reject a {self.status} campaign.")
        self.status = self.Status.REJECTED
        self.save(update_fields=["status", "updated_at"])

    def pause(self):
        if self.status != self.Status.APPROVED:
            raise ValueError("Only approved campaigns can be paused.")
        self.status = self.Status.PAUSED
        self.save(update_fields=["status", "updated_at"])

    def resume(self):
        if self.status != self.Status.PAUSED:
            raise ValueError("Only paused campaigns can be resumed.")
        self.status = self.Status.APPROVED
        self.save(update_fields=["status", "updated_at"])

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------
    @property
    def ctr(self):
        if not self.impressions:
            return 0
        return round((self.clicks / self.impressions) * 100, 2)

    # ------------------------------------------------------------------
    # Safe external link (defense in depth beyond URLValidator)
    # ------------------------------------------------------------------
    @property
    def safe_target_url(self):
        url = (self.target_url or "").strip()
        if url.lower().startswith(("http://", "https://")):
            return url
        return ""

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------
    def is_live(self):
        """True if the campaign should be shown to site visitors *right now*."""
        if self.status != self.Status.APPROVED:
            return False
        if not self.subscription or not self.subscription.is_current:
            return False
        if not (self.image or self.video):
            return False
        if not self.safe_target_url:
            return False
        now = timezone.now()
        if self.starts_at and self.starts_at > now:
            return False
        if self.ends_at and self.ends_at <= now:
            return False
        return True