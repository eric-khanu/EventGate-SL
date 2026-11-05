"""Forms for the accounts app."""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

User = get_user_model()


class SignUpForm(UserCreationForm):
    """Public sign-up. Always creates a plain viewer — never a role."""

    email = forms.EmailField(
        required=True,
        help_text="We'll send your tickets and receipts here.",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].help_text = (
            "150 characters or fewer. Letters, digits and @/./+/-/_ only."
        )
        self.fields["password1"].help_text = (
            "At least 8 characters. Cannot be entirely numeric."
        )
        self.fields["password2"].help_text = (
            "Enter the same password as above, for verification."
        )
        for name in ("password1", "password2"):
            self.fields[name].widget.attrs.setdefault(
                "autocomplete", "new-password",
            )

    def clean_username(self):
        return self.cleaned_data["username"].strip()

    def clean_email(self):
        """
        Best-effort uniqueness check.

        The stock ``User`` model does not enforce email uniqueness at the
        DB level, so a concurrent sign-up with the same email can still
        slip through. This pre-check catches the common case and gives a
        friendly error; it is not a security boundary.
        """
        email = self.cleaned_data["email"].strip().lower()
        if not email:
            raise forms.ValidationError("Enter a valid email address.")
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists."
            )
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        # Viewers only. Roles come from the super admin (Django admin ->
        # Groups) or, for scanners, from an organizer via AddScannerForm.
        # Never grant roles or staff/superuser flags at signup.
        user.is_staff = False
        user.is_superuser = False
        if commit:
            user.save()
        return user


# accounts/forms.py — append
from django.contrib.auth.forms import AuthenticationForm


class EventGateLoginForm(AuthenticationForm):
    """Authentication form with proper autocomplete hints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.setdefault("autocomplete", "username")
        self.fields["username"].widget.attrs.setdefault("autofocus", True)
        self.fields["password"].widget.attrs.setdefault(
            "autocomplete", "current-password",
        )