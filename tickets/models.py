from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)


# ===========================================================================
# Errors
# ===========================================================================
class EncryptionError(Exception):
    """Any failure in the encrypt/decrypt pipeline."""


class TicketTokenError(ValueError):
    """Raised when a ticket's encrypted token cannot be read or verified."""


class TicketStateError(ValueError):
    """Raised when a ticket state transition is not permitted."""


# ===========================================================================
# Key-versioned Fernet helpers
# ===========================================================================
# Settings shape:
#
#   TICKET_ENCRYPTION_KEYS = {
#       1: "urlsafe-b64-32-byte-key-1",
#       2: "urlsafe-b64-32-byte-key-2",
#   }
#   TICKET_ENCRYPTION_CURRENT_VERSION = 2
#
# Decryption uses the version recorded on the row; new encryption uses
# CURRENT_VERSION. Old keys stay valid for reading until you drop them.

_KEY_CACHE: dict[int, Fernet] = {}


def _load_keys() -> dict[int, Fernet]:
    """Parse and cache the configured key set. Fails fast on misconfig."""
    if _KEY_CACHE:
        return _KEY_CACHE

    raw = getattr(settings, "TICKET_ENCRYPTION_KEYS", None)
    if not raw:
        raise ImproperlyConfigured(
            "TICKET_ENCRYPTION_KEYS is not set. Generate keys with "
            "`Fernet.generate_key()` and put them in settings."
        )
    if not isinstance(raw, dict) or not raw:
        raise ImproperlyConfigured(
            "TICKET_ENCRYPTION_KEYS must be a non-empty dict "
            "{version: key_string}."
        )

    loaded: dict[int, Fernet] = {}
    for version, key in raw.items():
        if isinstance(key, str):
            key = key.encode("utf-8")
        try:
            loaded[int(version)] = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"TICKET_ENCRYPTION_KEYS[{version}] is not a valid Fernet key."
            ) from exc

    current = getattr(settings, "TICKET_ENCRYPTION_CURRENT_VERSION", None)
    if current not in loaded:
        raise ImproperlyConfigured(
            f"TICKET_ENCRYPTION_CURRENT_VERSION={current!r} is not present "
            f"in TICKET_ENCRYPTION_KEYS (have {sorted(loaded)})."
        )

    _KEY_CACHE.update(loaded)
    return _KEY_CACHE


def reset_fernet_cache() -> None:
    """Clear the cached Fernet instances (call after rotating keys at runtime)."""
    _KEY_CACHE.clear()


def current_key_version() -> int:
    return int(settings.TICKET_ENCRYPTION_CURRENT_VERSION)


def is_known_key_version(version: int) -> bool:
    """True if ``version`` is present in the configured keyring."""
    try:
        return int(version) in _load_keys()
    except ImproperlyConfigured:
        return False


def encrypt_token(raw: str, *, version: int | None = None) -> tuple[str, int]:
    """Encrypt a raw token with the current (or specified) key."""
    version = version or current_key_version()
    keys = _load_keys()
    if version not in keys:
        raise EncryptionError(
            f"Cannot encrypt with key version {version}: not configured."
        )
    try:
        ciphertext = keys[version].encrypt(raw.encode("utf-8")).decode("utf-8")
    except Exception as exc:                              # noqa: BLE001
        raise EncryptionError("Encrypt failed.") from exc
    return ciphertext, version


def decrypt_token(ciphertext: str, version: int) -> str:
    """Decrypt a stored token using the key version it was written with."""
    keys = _load_keys()
    if version not in keys:
        raise EncryptionError(
            f"Cannot decrypt ticket token: key version {version} is no "
            f"longer configured (have {sorted(keys)})."
        )
    try:
        return keys[version].decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError(
            f"Could not decrypt ticket token with key version {version} — "
            f"the ciphertext may be corrupted."
        ) from exc


def _default_key_version() -> int:
    """Deferred default so importing this module never touches settings."""
    return int(getattr(settings, "TICKET_ENCRYPTION_CURRENT_VERSION", 1))


# ===========================================================================
# Ticket
# ===========================================================================
class Ticket(models.Model):
    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        USED = "scanned", "Scanned"
        VOID = "void", "Voided"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    public_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        related_name="tickets",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tickets",
    )

    # ------------------------------------------------------------------
    # Encrypted token
    # ------------------------------------------------------------------
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="SHA-256 of the raw token; used for O(1) scan lookup.",
    )
    token_ciphertext = models.TextField(editable=False)
    token_key_version = models.PositiveSmallIntegerField(
        default=_default_key_version,
        editable=False,
        db_index=True,
        help_text="Which TICKET_ENCRYPTION_KEYS entry decrypts this ticket.",
    )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    status = models.CharField(
        max_length=8,
        choices=Status.choices,
        default=Status.VALID,
        db_index=True,
    )

    issued_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_scanned",
    )
    used_device_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-issued_at"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["order_item", "status"]),
            models.Index(fields=["token_key_version", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status="scanned", used_at__isnull=False)
                    | ~models.Q(status="scanned")
                ),
                name="ticket_used_at_set_when_used",
            ),
        ]

    # ==================================================================
    # Token helpers
    # ==================================================================
    @staticmethod
    def hash_token(token: str) -> str:
        """Stable hex digest of the raw token used as a lookup key."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # ==================================================================
    # Issuance
    # ==================================================================
    @classmethod
    def issue(cls, order_item, owner) -> tuple["Ticket", str]:
        """
        Create a Ticket, encrypt its raw token, and return ``(ticket, raw)``.

        ``raw`` is the only time the raw token is available in plaintext —
        the caller must render it into the QR and drop it.
        """
        last_error: Exception | None = None
        for _ in range(5):
            raw = secrets.token_urlsafe(32)
            token_hash = cls.hash_token(raw)
            try:
                with transaction.atomic():
                    ciphertext, version = encrypt_token(raw)
                    if not is_known_key_version(version):
                        raise EncryptionError(
                            f"Encrypt produced an unknown key version {version}."
                        )
                    obj = cls.objects.create(
                        order_item=order_item,
                        owner=owner,
                        token_hash=token_hash,
                        token_ciphertext=ciphertext,
                        token_key_version=version,
                    )
                return obj, raw
            except IntegrityError as exc:
                last_error = exc
                text = str(exc).lower()
                if "token_hash" not in text and "public_id" not in text:
                    raise
        raise IntegrityError(
            "Could not allocate a unique ticket token after 5 tries."
        ) from last_error

    # ==================================================================
    # Decryption — pure read, no side effects
    # ==================================================================
    def get_raw_token(self) -> str:
        """
        Decrypt and return the raw token.

        Pure read: no DB write, no key rotation, no invalidation of any
        outstanding QR. Re-encrypting here would silently break every
        screenshot of this ticket the moment its owner opened the page.

        Raises:
            TicketTokenError: ciphertext cannot be decrypted with the
                configured key version.
        """
        try:
            return decrypt_token(self.token_ciphertext, self.token_key_version)
        except EncryptionError as exc:
            logger.warning(
                "Ticket %s: decrypt failed (key_version=%s): %s",
                self.pk, self.token_key_version, exc,
            )
            raise TicketTokenError(
                "This ticket's secure code could not be verified. "
                "Please contact support and quote your ticket ID."
            ) from exc

    # ==================================================================
    # Explicit key rotation (deliberate, not a side effect of reading)
    # ==================================================================
    def rotate_key(self) -> bool:
        """
        Re-encrypt this ticket under the current key version.

        Returns ``True`` if the ticket was rotated, ``False`` if it was
        already current. Uses an optimistic lock: if a concurrent caller
        rotated it first, this is a no-op.

        **Invalidates any outstanding QR payload** for this ticket, because
        the QR signs ``token_key_version``. That's the point — call this
        during an incident response or a scheduled rotation, not from a
        view. Bulk rotation:

            Ticket.objects.filter(token_key_version__lt=current_key_version())

        then call ``rotate_key()`` on each — or use the management command
        in ``tickets.management.commands.rotate_ticket_keys`` if you build
        one.
        """
        try:
            current = current_key_version()
        except ImproperlyConfigured:
            return False
        if self.token_key_version == current:
            return False

        raw = self.get_raw_token()   # decrypt under old version
        ciphertext, version = encrypt_token(raw)

        updated = Ticket.objects.filter(
            pk=self.pk,
            token_key_version=self.token_key_version,  # optimistic lock
        ).update(
            token_ciphertext=ciphertext,
            token_key_version=version,
        )
        if not updated:
            return False

        self.token_ciphertext = ciphertext
        self.token_key_version = version
        return True

    # ==================================================================
    # State transitions
    # ==================================================================
    def mark_used(
        self, *,
        scanner,
        device_id: str = "",
        now=None,
    ) -> None:
        """
        Mark the ticket as used.

        Conditional update: only the request that flips ``status`` from
        VALID → USED succeeds, so concurrent scans can't both be accepted.
        """
        now = now or timezone.now()
        updated = Ticket.objects.filter(
            pk=self.pk,
            status=self.Status.VALID,
        ).update(
            status=self.Status.USED,
            used_at=now,
            used_by=scanner,
            used_device_id=device_id or "",
        )
        if not updated:
            current = Ticket.objects.only("status").get(pk=self.pk).status
            raise TicketStateError(f"Ticket is {current!r}, not valid.")

        self.status = self.Status.USED
        self.used_at = now
        self.used_by = scanner
        self.used_device_id = device_id or ""

    def void(self) -> None:
        """Void the ticket (organizer action). Idempotent; refuses used tickets."""
        if self.status == self.Status.VOID:
            return
        if self.status == self.Status.USED:
            raise TicketStateError("Cannot void an already-used ticket.")

        updated = Ticket.objects.filter(
            pk=self.pk,
            status=self.Status.VALID,
        ).update(status=self.Status.VOID)
        if not updated:
            current = Ticket.objects.only("status").get(pk=self.pk).status
            raise TicketStateError(f"Cannot void ticket in state {current!r}.")

        self.status = self.Status.VOID

    # ==================================================================
    # Validation
    # ==================================================================
    def clean(self) -> None:
        """Refuse to persist a ticket whose key version isn't configured."""
        if self.token_key_version and not is_known_key_version(self.token_key_version):
            raise ValidationError({
                "token_key_version":
                    f"Key version {self.token_key_version} is not present in "
                    "TICKET_ENCRYPTION_KEYS. Refusing to persist an unreadable ticket."
            })

    # ==================================================================
    # Convenience
    # ==================================================================
    @property
    def event(self):
        return self.order_item.tier.event

    def __str__(self) -> str:
        return f"Ticket {self.public_id} ({self.get_status_display()})"


# ===========================================================================
# ScanAttempt — every scan attempt (successful or not), append-only
# ===========================================================================
class ScanAttempt(models.Model):
    """
    Audit row for every scan attempt.

    One row per attempt, whether or not the ticket was actually consumed.
    A row is never updated or deleted — this is the source of truth for
    "who scanned what, when, and what the outcome was".

    ``ticket`` is nullable because a scan can fail *before* a ticket is
    identified (malformed token, unknown token hash). When the attempt
    reached a ticket, both ``ticket`` and ``event`` are set; otherwise
    ``event`` still records the event the scanner was operating under.
    """

    class Result(models.TextChoices):
        USED = "used", "Ticket used"
        ALREADY_USED = "already_used", "Already used"
        VOID = "void", "Voided"
        EXPIRED = "expired", "Expired"
        TOO_EARLY = "too_early", "Too early"
        EVENT_ENDED = "event_ended", "Event ended"
        EVENT_INACTIVE = "event_inactive", "Event inactive"
        WRONG_EVENT = "wrong_event", "Wrong event"
        NOT_FOUND = "not_found", "Not found"
        INVALID = "invalid", "Invalid token"
        RATE_LIMITED = "rate_limited", "Rate limited"
        ERROR = "error", "Server error"

        @classmethod
        def unmatched(cls) -> list[str]:
            """Result *values* that legitimately have no associated ticket."""
            return [cls.NOT_FOUND, cls.INVALID, cls.ERROR]

    # ---- Foreign keys ----
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.PROTECT,
        related_name="scan_attempts",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_attempts",
    )
    scanner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scan_attempts",
    )

    # ---- Outcome ----
    result = models.CharField(
        max_length=20,
        choices=Result.choices,
        db_index=True,
    )
    ok = models.BooleanField(db_index=True)

    # ---- Context (never trust these as security signals) ----
    device_id = models.CharField(max_length=100, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    raw_token_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text="SHA-256 of the submitted token. Never the raw token.",
    )

    # ---- When ----
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event", "created_at"]),
            models.Index(fields=["scanner", "created_at"]),
            models.Index(fields=["result", "created_at"]),
        ]
        # Raw string literals, not ``Result.X`` — ``Result`` is a nested
        # class and is not visible in ``Meta`` in a stable way. Django
        # serialises these constraints with the raw value anyway.
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(ok=True, result="used")
                    | models.Q(ok=False)
                ),
                name="scanattempt_ok_only_when_used",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ticket__isnull=False)
                    | models.Q(result__in=["not_found", "invalid", "error"])
                ),
                name="scanattempt_ticket_required_unless_unmatched",
            ),
        ]

    def clean(self) -> None:
        if self.ticket_id and self.event_id and self.ticket.event_id != self.event_id:
            raise ValidationError(
                {"event": "ScanAttempt.event must match the ticket's event."}
            )

    def __str__(self) -> str:
        return f"{self.get_result_display()} @ {self.created_at:%Y-%m-%d %H:%M}"

# ===========================================================================
# ScanLog
# ===========================================================================
class ScanLog(models.Model):
    """Audit row for every scan attempt (successful or not)."""

    class Result(models.TextChoices):
        VALID = "valid", "Valid"
        ALREADY_USED = "already_used", "Already used"
        VOID = "void", "Voided"
        NOT_FOUND = "not_found", "Not found"
        WRONG_EVENT = "wrong_event", "Wrong event"
        ERROR = "error", "Error"

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.PROTECT,
        related_name="scan_logs",
        null=True,
        blank=True,
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.PROTECT,
        related_name="scan_logs",
    )
    scanner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scan_logs",
    )
    device_id = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=20, choices=Result.choices)
    detail = models.CharField(max_length=250, blank=True)
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-scanned_at"]
        indexes = [
            models.Index(fields=["event", "scanned_at"]),
            models.Index(fields=["result", "scanned_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(ticket__isnull=False)
                    | models.Q(result__in=["not_found", "error"])
                ),
                name="scanlog_ticket_required_unless_not_found_or_error",
            ),
        ]

    def clean(self) -> None:
        if self.ticket_id and self.event_id and self.ticket.event_id != self.event_id:
            raise ValidationError(
                {"event": "ScanLog.event must match the ticket's event."}
            )

    def __str__(self) -> str:
        return f"{self.result} @ {self.scanned_at:%Y-%m-%d %H:%M}"

