from django import forms
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import Event, EventVideo, TicketTier


# ---------------------------------------------------------------------------
# Reusable widgets / helpers
# ---------------------------------------------------------------------------
def _datetime_widget():
    
    return forms.DateTimeInput(
        attrs={"type": "datetime-local"},
        format="%Y-%m-%dT%H:%M",
    )


def _sniff_video(f) -> bool:
    """Return True if the first bytes of ``f`` look like MP4 or WebM."""
    try:
        f.seek(0)
        head = f.read(16)
    finally:
        f.seek(0)
    # MP4: 'ftyp' at offset 4; WebM: EBML magic 1A 45 DF A3 at offset 0.
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3"


# ---------------------------------------------------------------------------
# Event form
# ---------------------------------------------------------------------------
class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = [
            "title",
            "summary",
            "description",
            "category",
            "venue",
            "district",
            "region",
            "starts_at",
            "ends_at",
            "cover_image",
            "contact_phone",
            "currency",
        ]
        widgets = {
            "starts_at": _datetime_widget(),
            "ends_at": _datetime_widget(),
            "description": forms.Textarea(attrs={"rows": 6}),
            "summary": forms.TextInput(attrs={"maxlength": 300}),
        }

    def clean_cover_image(self):
      
        f = self.cleaned_data.get("cover_image")
        if not f or not hasattr(f, "read"):
            return f
        try:
            f.seek(0)
            with Image.open(f) as im:
                im.verify()
        except (UnidentifiedImageError, OSError):
            raise forms.ValidationError("Not a valid image file.")
        finally:
            f.seek(0)
        return f

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")

        if starts_at and ends_at and ends_at <= starts_at:
            self.add_error("ends_at", "End time must be after start time.")

        # Only reject a *past* start time when creating — organizers must be
        # able to edit a historical draft, but a brand-new event starting in
        # the past is almost certainly a mistake.
        if (
            not self.instance.pk
            and starts_at
            and starts_at <= timezone.now()
        ):
            self.add_error("starts_at", "Start time must be in the future.")

        return cleaned


# ---------------------------------------------------------------------------
# Ticket tier form
# ---------------------------------------------------------------------------
class TierForm(forms.ModelForm):
    class Meta:
        model = TicketTier
        fields = [
            "name",
            "description",
            "price",
            "quantity",
            "discount_percent",
            "discount_starts_at",
            "discount_ends_at",
            "sale_starts",
            "sale_ends",
            "is_active",
        ]
        widgets = {
            "discount_starts_at": _datetime_widget(),
            "discount_ends_at": _datetime_widget(),
            "sale_starts": _datetime_widget(),
            "sale_ends": _datetime_widget(),
        }

    def __init__(self, *args, event=None, organizer=None, **kwargs):
      
        self.event = event
        self.organizer = organizer
        super().__init__(*args, **kwargs)

        if (
            event is not None
            and organizer is not None
            and event.organizer_id != organizer.pk
        ):
            raise forms.ValidationError("Not your event.")

        # Seed sensible defaults from the event on *create* only. Editing
        # an existing tier must preserve whatever dates are already stored.
        if self.event is not None and not self.instance.pk:
            if not self.initial.get("sale_starts"):
                self.initial["sale_starts"] = timezone.now()
            if not self.initial.get("sale_ends"):
                self.initial["sale_ends"] = self.event.starts_at

    # -- field-level clean --------------------------------------------------
    def clean_price(self):
        price = self.cleaned_data["price"]
        if price is not None and price < 0:
            raise forms.ValidationError("Price cannot be negative.")
        return price

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity is None:
            return quantity
        sold = getattr(self.instance, "sold", 0) or 0
        if quantity < sold:
            raise forms.ValidationError(
                f"Cannot set quantity below already-sold ({sold})."
            )
        return quantity

    def clean_discount_percent(self):
        percent = self.cleaned_data["discount_percent"]
        if percent is None:
            return percent
        if percent < 0 or percent > 100:
            raise forms.ValidationError(
                "Discount must be between 0 and 100."
            )
        return percent

    # -- cross-field clean --------------------------------------------------
    def clean(self):
        cleaned = super().clean()
        errors = {}

        d_start = cleaned.get("discount_starts_at")
        d_end = cleaned.get("discount_ends_at")
        s_start = cleaned.get("sale_starts")
        s_end = cleaned.get("sale_ends")
        discount_percent = cleaned.get("discount_percent")

        has_window = bool(d_start or d_end)
        has_percent = discount_percent is not None and discount_percent > 0

        # --- Early-bird window ---
        if d_start and d_end and d_end <= d_start:
            errors["discount_ends_at"] = (
                "Early-bird discount must end after it starts."
            )

        # Window ⇔ positive percent (both directions).
        if has_window and not has_percent:
            errors["discount_percent"] = (
                "Set a discount percentage to use an early-bird window."
            )
        if has_percent and not has_window:
            errors["discount_starts_at"] = (
                "A discount percentage requires an early-bird window."
            )

        # --- Sale window ---
        if s_start and s_end and s_end <= s_start:
            errors["sale_ends"] = "Sale must end after it starts."

        # --- Cross-window consistency ---
        if d_end and s_end and d_end > s_end:
            errors["discount_ends_at"] = (
                "Early-bird window must end no later than the sale window."
            )
        if d_start and s_end and d_start >= s_end:
            errors["discount_starts_at"] = (
                "Early-bird window must start before the sale ends."
            )

        # --- Cross-form consistency (event boundaries) ---
        if self.event is not None:
            if s_start and s_start >= self.event.ends_at:
                errors["sale_starts"] = (
                    "Sale cannot start after the event has ended."
                )
            if s_end and s_end > self.event.ends_at:
                errors["sale_ends"] = (
                    "Sale cannot end after the event has ended."
                )
            if d_start and d_start < (s_start or timezone.now()):
                # A discount that starts before the sale begins is almost
                # always a copy-paste mistake.
                errors["discount_starts_at"] = (
                    "Early-bird window cannot start before the sale begins."
                )

        for field, message in errors.items():
            self.add_error(field, message)

        return cleaned


# ---------------------------------------------------------------------------
# Event video form (inline under EventForm)
# ---------------------------------------------------------------------------
class EventVideoForm(forms.ModelForm):
    class Meta:
        model = EventVideo
        fields = ["order", "title", "source", "url", "file"]
        widgets = {
            "order": forms.NumberInput(attrs={"min": 0}),
        }

    def clean_file(self):
        """Reject files whose content isn't MP4 or WebM, regardless of extension."""
        f = self.cleaned_data.get("file")
        if not f or not hasattr(f, "read"):
            return f
        if not _sniff_video(f):
            raise forms.ValidationError(
                "File does not look like a valid MP4 or WebM video."
            )
        return f

    def clean(self):
        cleaned = super().clean()
        url = cleaned.get("url")
        file = cleaned.get("file")
        source = cleaned.get("source")

        # All errors are field-scoped so the inline row highlights the
        # offending input rather than showing a banner at the form level.
        if not url and not file:
            raise forms.ValidationError(
                {
                    "url": "Provide either a video URL or an uploaded file.",
                    "file": "Provide either a video URL or an uploaded file.",
                }
            )

        if url and file:
            raise forms.ValidationError(
                {
                    "url": "Provide only a URL or an uploaded file, not both.",
                    "file": "Provide only a URL or an uploaded file, not both.",
                }
            )

        if source == EventVideo.Source.UPLOAD and not file:
            raise forms.ValidationError(
                {"file": "Upload a file when source is 'Uploaded file'."}
            )

        if source in {
            EventVideo.Source.YOUTUBE,
            EventVideo.Source.VIMEO,
        } and not url:
            raise forms.ValidationError(
                {"url": f"A URL is required for {source} videos."}
            )

        return cleaned


# ---------------------------------------------------------------------------
# Add scanner (organizer-only)
# ---------------------------------------------------------------------------
class AddScannerForm(forms.Form):
    """Organizer adds an existing user as a scanner for one of their events."""

    event = forms.ModelChoiceField(queryset=Event.objects.none())
    username = forms.CharField(
        max_length=150,
        help_text="Existing EventGate username of the person scanning.",
    )

    def __init__(self, *args, organizer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organizer = organizer
        self.scanner = None
        self.fields["event"].queryset = (
            Event.objects
            .filter(organizer=organizer)
            .exclude(status=Event.Status.CANCELLED)
            .order_by("-starts_at")
        )

    def clean_username(self):
        from django.contrib.auth import get_user_model
        username = self.cleaned_data["username"].strip()
        User = get_user_model()
        try:
            self.scanner = User.objects.get(username=username)
        except User.DoesNotExist:
            raise forms.ValidationError("No user with that username.")
        if self.scanner.groups.filter(name="Organizers").exists():
            raise forms.ValidationError(
                "Organizers already have scanning rights on their own events."
            )
        return username

    def clean(self):
        cleaned = super().clean()
        event = cleaned.get("event")
        if event and self.organizer and event.organizer_id != self.organizer.pk:
            self.add_error("event", "You can only assign scanners to your own events.")
        return cleaned

    def save(self):
        from .models import EventScanner
        obj, _ = EventScanner.objects.get_or_create(
            event=self.cleaned_data["event"],
            user=self.scanner,
            defaults={"created_by": self.organizer},
        )
        return obj


from django import forms
from django.contrib.auth import get_user_model

from accounts.permissions import has_scanner_role
from .models import EventScanner

User = get_user_model()


class AddScannerForm(forms.ModelForm):
    """
    Resolve a username/email to a user, validate the scanner role, and
    guard against duplicate (event, user) assignments at the form layer.
    """

    identifier = forms.CharField(
        label="Username or email",
        max_length=254,
        help_text="The person must already have an EventGate account.",
    )

    class Meta:
        model = EventScanner
        fields = ()  # we only accept `identifier`; event/user are set by the view

    def __init__(self, *args, event=None, organizer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.organizer = organizer

    def clean_identifier(self):
        raw = (self.cleaned_data["identifier"] or "").strip()
        if not raw:
            raise forms.ValidationError("Enter a username or email address.")

        user = (
            User.objects.filter(username__iexact=raw).first()
            or User.objects.filter(email__iexact=raw).first()
        )
        if user is None:
            raise forms.ValidationError(f'No user matches "{raw}".')

        if self.organizer and user.pk == self.organizer.pk:
            raise forms.ValidationError(
                "You are already the organizer of this event."
            )

        if not has_scanner_role(user):
            raise forms.ValidationError(
                f"{user.username} does not have the 'scanner' role. "
                f"Ask an administrator to grant it first."
            )

        if self.event and EventScanner.objects.filter(
            event=self.event, user=user,
        ).exists():
            raise forms.ValidationError(
                f"{user.username} is already a scanner for this event."
            )

        self._resolved_user = user
        return raw

    def save(self, commit=True):
        scanner = super().save(commit=False)
        scanner.user = self._resolved_user
        scanner.event = self.event
        if commit:
            scanner.save()
        return scanner