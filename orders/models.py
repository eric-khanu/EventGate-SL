import logging
import mimetypes  # kept for tooling; not relied on for security
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.urls import reverse
from django.utils import timezone

from events.models import TicketTier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_PAYMENT_PROOF_BYTES = 5 * 1024 * 1024

ALLOWED_PAYMENT_PROOF_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

ALLOWED_PAYMENT_PROOF_CONTENT_TYPES = set(ALLOWED_PAYMENT_PROOF_TYPES) | {""}

# Number of bytes libmagic needs for reliable type detection.
_MAGIC_SNIFF_BYTES = 8192


# ---------------------------------------------------------------------------
# Upload path
# ---------------------------------------------------------------------------
def payment_proof_upload_to(instance, filename):
    """
    Store proofs under ``media/payment_proofs/<year>/<month>/<order_ref>/proof<ext>``.

    The extension is chosen from the *detected* content type (see
    :meth:`Order._validate_payment_proof`), never from the attacker-supplied
    filename. That's why this function ignores ``filename`` — the caller is
    expected to set ``uploaded_file.name = "proof<safe_ext>"`` before saving.
    """
    now = timezone.now()
    ref = getattr(instance, "reference", None) or uuid.uuid4()
    # If the caller rewrote the name to include a safe extension, honour it.
    suffix = Path(filename).suffix if filename else ""
    if suffix not in set(ALLOWED_PAYMENT_PROOF_TYPES.values()):
        suffix = ""
    return f"payment_proofs/{now:%Y/%m}/{ref}/proof{suffix}"


# ---------------------------------------------------------------------------
# MIME detection — mandatory, no extension fallback
# ---------------------------------------------------------------------------
def _detect_content_type(uploaded_file):
    """
    Return the MIME type detected from the file's magic bytes.

    Requires ``python-magic`` (and the OS ``libmagic``). If it is not
    installed, raise ``ValidationError`` rather than degrading to a
    filename-extension guess — extension-based checks are attacker-controlled.
    """
    try:
        import magic
    except ImportError as exc:
        raise ValidationError(
            "Server is missing the 'python-magic' dependency — payment "
            "proof uploads are disabled. Ask the administrator to install it."
        ) from exc

    uploaded_file.seek(0)
    mime = magic.from_buffer(uploaded_file.read(_MAGIC_SNIFF_BYTES), mime=True)
    uploaded_file.seek(0)
    return mime


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------
class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment verification"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Payment rejected"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    class PaymentMethod(models.TextChoices):
        ORANGE_MONEY = "orange_money", "Orange Money"
        AFRIMONEY = "afrimoney", "Afrimoney"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CASH = "cash", "Cash / at venue"

    reference = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ticket_orders",
    )
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    currency = models.CharField(max_length=3, default="SLE")
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )

    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
    )
    payment_reference = models.CharField(max_length=120, blank=True)
    payment_note = models.TextField(blank=True)

    payment_proof = models.FileField(
        upload_to=payment_proof_upload_to,
        blank=True,
        null=True,
        help_text="Uploaded screenshot/receipt of the payment.",
    )
    payment_proof_content_type = models.CharField(
        max_length=100,
        blank=True,
        help_text="MIME type detected on upload (e.g. image/png).",
    )
    payment_proof_uploaded_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["buyer", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total__gte=0),
                name="order_total_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=["paid", "refunded"], paid_at__isnull=False)
                    | ~Q(status__in=["paid", "refunded"])
                ),
                name="order_paid_at_set_when_paid",
            ),
            # The stored content type must be one of the allowed values, or
            # empty for legacy rows. Prevents an admin (or a bug) from setting
            # it to `text/html` and having the download view serve XSS.
            models.CheckConstraint(
                condition=Q(payment_proof_content_type__in=list(ALLOWED_PAYMENT_PROOF_CONTENT_TYPES)),
                name="order_payment_proof_content_type_whitelist",
            ),
        ]

    def __str__(self):
        return str(self.reference)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    @property
    def ticket_count(self) -> int:
        """Number of tickets this order will issue (= sum of item quantities)."""
        return sum(item.quantity for item in self.items.all())

    # ------------------------------------------------------------------
    # Payment proof handling
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_payment_proof(uploaded_file):
        """
        Validate size and type; return the detected MIME type.

        Raises ``ValidationError`` on failure so forms/admin can surface it.
        Rewrites ``uploaded_file.name`` to ``proof<safe_ext>`` so the storage
        layer picks a canonical extension — never the user-supplied one.
        """
        if uploaded_file.size > MAX_PAYMENT_PROOF_BYTES:
            raise ValidationError(
                f"Payment proof is too large "
                f"({uploaded_file.size} bytes); max is "
                f"{MAX_PAYMENT_PROOF_BYTES} bytes."
            )

        content_type = _detect_content_type(uploaded_file)
        if content_type not in ALLOWED_PAYMENT_PROOF_TYPES:
            raise ValidationError(
                f"Unsupported payment proof type: {content_type}. "
                f"Allowed: {sorted(ALLOWED_PAYMENT_PROOF_TYPES)}"
            )

        # Force the canonical extension. This is what `payment_proof_upload_to`
        # relies on when it derives the path — the extension must come from
        # the whitelist, never from the upload's original filename.
        canonical_ext = ALLOWED_PAYMENT_PROOF_TYPES[content_type]
        uploaded_file.name = f"proof{canonical_ext}"
        return content_type

    def attach_payment_proof(self, uploaded_file, *, replace=True):
        """
        Validate, store the file, and record metadata.

        If ``replace`` is False and a proof already exists, this is a no-op.

        Any previously stored file is deleted **after** the new one commits,
        so a failed save never leaves the order without a proof. The DB row
        and the file on disk are always written together — unlike a bare
        ``.update(payment_proof=...)`` which writes only the field name.
        """
        if self.pk is None:
            raise ValueError(
                "attach_payment_proof() requires a saved Order (pk is None)."
            )

        if not replace and self.payment_proof:
            return

        content_type = self._validate_payment_proof(uploaded_file)
        old_name = self.payment_proof.name if self.payment_proof else None

        with transaction.atomic():
            # Route the write through the FileField so Storage.save() runs
            # and the actual bytes are persisted to MEDIA_ROOT (or S3, …).
            self.payment_proof.save(
                uploaded_file.name,
                uploaded_file,
                save=False,
            )
            self.payment_proof_content_type = content_type
            self.payment_proof_uploaded_at = timezone.now()
            self.save(
                update_fields=[
                    "payment_proof",
                    "payment_proof_content_type",
                    "payment_proof_uploaded_at",
                ]
            )

        # Old file cleanup AFTER commit so a rolled-back save never orphans
        # the replacement. Best-effort: a missing file must not break the flow.
        if old_name and old_name != self.payment_proof.name:
            try:
                self.payment_proof.storage.delete(old_name)
            except Exception:                     # noqa: BLE001
                logger.warning(
                    "Could not delete superseded payment proof %r for order %s",
                    old_name, self.pk, exc_info=True,
                )

    @property
    def has_payment_proof_file(self) -> bool:
        """True only if the DB reference AND the file on disk both exist."""
        pf = self.payment_proof
        if not pf:
            return False
        try:
            return pf.storage.exists(pf.name)
        except Exception:                         # noqa: BLE001
            # A remote storage backend can raise on `exists()`; treat that
            # as "not sure" rather than crashing the template.
            logger.warning(
                "storage.exists() failed for %r (order %s)",
                pf.name, self.pk, exc_info=True,
            )
            return False

    @property
    def payment_proof_url(self):
        if not self.payment_proof:
            return ""
        return reverse("orders:download_payment_proof", args=[self.reference])

    def payment_proof_filename(self):
        """
        Suggested filename for the ``Content-Disposition`` header.

        Uses the content type recorded at upload — the stored filename has no
        extension by design, so we can't derive it from there.
        """
        ext = ALLOWED_PAYMENT_PROOF_TYPES.get(
            self.payment_proof_content_type or "",
            ".bin",
        )
        return f"payment_proof_{self.reference}{ext}"

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def mark_paid(self, *, paid_at=None, actor=None):
        """
        Transition to PAID and issue one Ticket per unit purchased.

        Idempotent: re-calling on an already-PAID order returns ``[]`` and
        issues no further tickets. The row is locked with ``select_for_update``
        so two concurrent callers cannot both issue tickets.
        """
        from tickets.models import Ticket  # avoid circular import

        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=self.pk)
            items = list(locked.items.select_related("tier__event"))

            # Integrity check: refuse to mark paid if line totals don't
            # reconcile with the stored order total.
            computed_total = sum((i.line_total for i in items), Decimal("0"))
            if computed_total != locked.total:
                raise ValueError(
                    f"Order {locked.reference} totals are inconsistent: "
                    f"stored {locked.total}, computed {computed_total}. "
                    f"Refusing to mark paid."
                )

            if locked.status == self.Status.PAID:
                self.status = locked.status
                self.paid_at = locked.paid_at
                return []

            if locked.status != self.Status.PENDING:
                raise ValueError(
                    f"Cannot mark a {locked.status} order as paid."
                )

            when = paid_at or timezone.now()
            locked.status = self.Status.PAID
            locked.paid_at = when
            locked.save(update_fields=["status", "paid_at"])

            issued = []
            for item in items:
                for _ in range(item.quantity):
                    ticket, _raw = Ticket.issue(
                        order_item=item,
                        owner=locked.buyer,
                    )
                    issued.append(ticket)

            self.status = locked.status
            self.paid_at = locked.paid_at
            return issued

    def _restock_locked_items(self, items):
        """
        Return reserved stock to the tiers. Clamped at zero so a tampered
        tier can't drive `sold` negative and trip a future constraint.
        """
        for item in items:
            TicketTier.objects.filter(pk=item.tier_id).update(
                sold=Greatest(F("sold") - item.quantity, 0),
            )

    def mark_rejected(self, *, actor=None):
        """Transition PENDING → REJECTED and restore reserved stock."""
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PENDING:
                raise ValueError("Only pending orders can be rejected.")

            self._restock_locked_items(
                locked.items.select_related("tier")
            )

            locked.status = self.Status.REJECTED
            locked.save(update_fields=["status"])

            self.status = locked.status

    def mark_cancelled(self, *, actor=None):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=self.pk)
            if locked.status not in (self.Status.PENDING, self.Status.PAID):
                raise ValueError("Only pending or paid orders can be cancelled.")

            if locked.status == self.Status.PENDING:
                # Restore stock — tickets were reserved at order creation
                # and no Ticket rows exist yet.
                self._restock_locked_items(
                    locked.items.select_related("tier")
                )

            locked.status = self.Status.CANCELLED
            locked.save(update_fields=["status"])

            self.status = locked.status

    def mark_refunded(self, *, actor=None):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=self.pk)
            if locked.status != self.Status.PAID:
                raise ValueError("Only paid orders can be refunded.")

            # Do NOT restore tier stock by default — the buyer may have used
            # their tickets. Refund + restock is a policy decision; implement
            # it as a separate `mark_refunded_and_restock` if that's the policy.

            locked.status = self.Status.REFUNDED
            locked.save(update_fields=["status"])

            self.status = locked.status


# ---------------------------------------------------------------------------
# OrderItem
# ---------------------------------------------------------------------------
class OrderItem(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="items",
    )
    tier = models.ForeignKey(
        TicketTier,
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    quantity = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    line_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "tier"],
                name="orderitem_unique_order_tier",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1),
                name="orderitem_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(line_total=F("unit_price") * F("quantity")),
                name="orderitem_line_total_matches",
            ),
        ]

    def __str__(self):
        return f"{self.tier.name} × {self.quantity}"

    def save(self, *args, **kwargs):
        expected = (self.unit_price or 0) * (self.quantity or 0)
        if self.line_total is None:
            self.line_total = expected
        elif self.line_total != expected:
            raise ValidationError(
                f"line_total {self.line_total} does not equal "
                f"unit_price × quantity ({expected})."
            )
        super().save(*args, **kwargs)