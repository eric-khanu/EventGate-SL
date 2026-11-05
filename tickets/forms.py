"""Forms for the tickets app.

Currently hosts:

* ``CheckoutForm`` — validates the checkout page's inputs, including the
  optional payment proof upload.
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from orders.models import Order


# ---------------------------------------------------------------------------
# Shared widget config
# ---------------------------------------------------------------------------
PAYMENT_PROOF_ACCEPT = "image/jpeg,image/png,image/webp,application/pdf"

# Payment methods that require a reference to reconcile against a statement.
METHODS_REQUIRING_REFERENCE = {
    Order.PaymentMethod.BANK_TRANSFER,
    Order.PaymentMethod.ORANGE_MONEY,
    Order.PaymentMethod.AFRIMONEY,
}


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
class CheckoutForm(forms.Form):
    """
    Validates the checkout page's inputs.

    ``tier`` and ``user`` are passed from the view so the form can tune
    ``quantity``'s upper bound and pre-fill the buyer's email. Both default
    to ``None`` to keep the form constructible in tests that only care
    about field definitions.
    """

    quantity = forms.IntegerField(min_value=1, max_value=10)
    email = forms.EmailField()
    phone = forms.CharField(max_length=30)
    payment_method = forms.ChoiceField(choices=Order.PaymentMethod.choices)
    payment_reference = forms.CharField(required=False, max_length=120)
    payment_proof = forms.FileField(
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": PAYMENT_PROOF_ACCEPT}),
    )
    payment_note = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "Transaction ID / sender name"},
        ),
    )

    def __init__(self, *args, tier=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tier = tier
        self.user = user

        if tier is not None:
            # Clamp to what's actually left. If remaining is 0, leave
            # max_value at 0 so ``is_valid`` rejects; ``clean_quantity``
            # will surface a clearer "sold out" message in that case.
            self.fields["quantity"].max_value = min(10, tier.remaining)

        if user is not None and not self.is_bound and user.is_authenticated:
            self.fields["email"].initial = user.email
            # Phone too, if the account has one.
            phone = getattr(user, "phone", "") or ""
            if phone:
                self.fields["phone"].initial = phone

    # ---- field-level clean -------------------------------------------------
    def clean_quantity(self) -> int:
        qty = self.cleaned_data["quantity"]
        # ``max_value`` is set per-tier in __init__, but ``IntegerField``
        # rejects above max with the confusing "less than or equal to 0"
        # when max_value is 0. Surface a clearer message here.
        if self.tier is not None and self.tier.remaining <= 0:
            raise forms.ValidationError(
                "This ticket tier is sold out. Please choose another tier."
            )
        return qty

    def clean_phone(self) -> str:
        phone = (self.cleaned_data.get("phone") or "").strip()
        if not phone:
            raise forms.ValidationError("Enter a phone number.")
        return phone

    def clean_payment_proof(self):
        f = self.cleaned_data.get("payment_proof")
        if f:
            # Ensure the cursor is at the start in case a prior validator
            # peeked at the file header and didn't rewind.
            f.seek(0)
            # Reuse the model's validator so size + MIME rules match
            # everywhere. Guard against non-ValidationError exceptions
            # (e.g. a missing python-magic install) becoming 500s.
            try:
                Order._validate_payment_proof(f)
            except ValidationError:
                raise
            except Exception:
                # Do not leak the underlying error; the ticket creation
                # path logs it, the user just needs "try another file".
                raise forms.ValidationError(
                    "This file could not be processed. "
                    "Please upload a JPEG, PNG, WEBP, or PDF under 5 MB."
                )
        return f

    # ---- form-level clean --------------------------------------------------
    def clean(self):
        data = super().clean()
        method = data.get("payment_method")
        ref = (data.get("payment_reference") or "").strip()

        if method in METHODS_REQUIRING_REFERENCE and not ref:
            self.add_error(
                "payment_reference",
                "A payment reference is required for this payment method.",
            )
        if method == Order.PaymentMethod.CASH and ref:
            # Cash payments don't have a provider reference — if they typed
            # something, it's probably a mistake or a note mis-entered.
            self.add_error(
                "payment_reference",
                "Cash payments don't have a reference; leave this blank.",
            )

        return data