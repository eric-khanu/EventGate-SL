"""Forms for advertisement subscriptions and campaign creatives."""

from django import forms
from django.core.exceptions import ValidationError

from .models import AdCampaign, AdSubscription


_INPUT_CLASSES = {"class": "w-full rounded-lg border border-slate-300 p-3"}


def _style_fields(form):
    for field in form.fields.values():
        field.widget.attrs.update(_INPUT_CLASSES)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------
class AdSubscriptionForm(forms.ModelForm):
    class Meta:
        model = AdSubscription
        fields = [
            "plan",
            "payment_method",
            "payment_reference",
            "payment_notes",
            "payment_proof",
        ]
        widgets = {
            "payment_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = self.fields["plan"].queryset.filter(
            is_active=True,
        )
        _style_fields(self)

    def clean_payment_proof(self):
        """Validate via the model path so MIME/extension rules match checkout."""
        f = self.cleaned_data.get("payment_proof")
        if f and hasattr(f, "read"):
            f.seek(0)
            AdSubscription._validate_payment_proof(f)
        return f

    def clean(self):
        cleaned = super().clean()
        plan = cleaned.get("plan")
        method = cleaned.get("payment_method")
        reference = (cleaned.get("payment_reference") or "").strip()
        proof = cleaned.get("payment_proof")

        if plan and not plan.is_active:
            self.add_error("plan", "This plan is no longer available.")

        if method and method != AdSubscription.PaymentMethod.OTHER:
            if not proof and not self.instance.payment_proof:
                self.add_error(
                    "payment_proof",
                    "Upload a receipt or screenshot of your payment.",
                )

        if not reference:
            self.add_error(
                "payment_reference",
                "Enter the transaction ID from your payment provider.",
            )
        return cleaned


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------
class AdCampaignForm(forms.ModelForm):
    class Meta:
        model = AdCampaign
        fields = [
            "company_name",
            "title",
            "caption",
            "image",
            "video",
            "target_url",
            "placement",
        ]
        widgets = {"caption": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_fields(self)

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if image and hasattr(image, "read"):
            image.seek(0)
            AdCampaign._validate_image(image)
        return image

    def clean_video(self):
        video = self.cleaned_data.get("video")
        if video and hasattr(video, "read"):
            video.seek(0)
            AdCampaign._validate_video(video)
        return video

    def clean_target_url(self):
        url = (self.cleaned_data.get("target_url") or "").strip()
        # URLValidator on the model handles this, but a friendly error here
        # is nicer than a generic form error.
        if url and not url.lower().startswith(("http://", "https://")):
            raise ValidationError("URL must start with http:// or https://.")
        return url

    def clean(self):
        cleaned = super().clean()
        image = cleaned.get("image")
        video = cleaned.get("video")

        has_image = image or (self.instance.pk and self.instance.image)
        has_video = video or (self.instance.pk and self.instance.video)

        if not has_image and not has_video:
            raise ValidationError(
                "Upload at least one image or short video creative."
            )
        return cleaned