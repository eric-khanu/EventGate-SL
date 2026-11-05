
from django import forms
from .models import Order


class PaymentProofForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("payment_proof",)
        widgets = {
            "payment_proof": forms.ClearableFileInput(
                attrs={"accept": "image/*,application/pdf"}
            ),
        }

    def clean_payment_proof(self):
        proof = self.cleaned_data.get("payment_proof")
        if proof is None:
            return proof  # allow keeping existing on edit
        max_mb = 10
        if proof.size > max_mb * 1024 * 1024:
            raise forms.ValidationError(f"File must be under {max_mb} MB.")
        allowed = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
        content_type = getattr(proof, "content_type", None)
        if content_type and content_type not in allowed:
            raise forms.ValidationError(
                "Upload a JPG, PNG, WEBP image or a PDF."
            )
        return proof