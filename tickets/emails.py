from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils.html import strip_tags

from .models import TicketTokenError
from .qr import qr_png_bytes, QRGenerationError

logger = logging.getLogger(__name__)


def _absolute_url(path: str) -> str:
    base = getattr(settings, "SITE_BASE_URL", "").rstrip("/")
    return f"{base}{path}"


def send_ticket_email(ticket) -> None:
    """Render and send one ticket with its QR attached."""
    order = ticket.order_item.order
    try:
        raw = ticket.get_raw_token()
    except TicketTokenError:
        raise RuntimeError(
            f"Ticket {ticket.public_id} cannot be decrypted — "
            f"contact support."
        )

    try:
        png = qr_png_bytes(ticket, raw)
    except QRGenerationError:
        raise RuntimeError(
            f"Ticket {ticket.public_id} QR could not be generated."
        )

    ticket_url = _absolute_url(
        reverse("tickets:ticket_detail", kwargs={"public_id": ticket.public_id})
    )

    subject = f"Your ticket for {ticket.event.title}"
    text = (
        f"Your ticket is attached as a QR image.\n\n"
        f"Event: {ticket.event.title}\n"
        f"Venue: {ticket.event.venue}\n"
        f"Starts: {ticket.event.starts_at:%Y-%m-%d %H:%M}\n\n"
        f"View it online: {ticket_url}\n\n"
        f"Ticket ID: {ticket.public_id}\n"
    )
    html = (
        f"<p>Your ticket for <strong>{ticket.event.title}</strong> "
        f"is attached.</p>"
        f"<p>Event: {ticket.event.title}<br>"
        f"Venue: {ticket.event.venue}<br>"
        f"Starts: {ticket.event.starts_at:%Y-%m-%d %H:%M}</p>"
        f"<p><a href='{ticket_url}'>View it online</a></p>"
        f"<p>Ticket ID: {ticket.public_id}</p>"
    )

    msg = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(text),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.email],
    )
    msg.attach_alternative(html, "text/html")
    msg.attach(f"ticket-{ticket.public_id}.png", png, "image/png")
    msg.send(fail_silently=False)


def send_rejection_email(order) -> None:
    """Notify the buyer that their payment could not be verified."""
    subject = f"Payment not verified — order {order.reference}"
    body = (
        f"Hi,\n\n"
        f"We could not verify the payment for your order "
        f"{order.reference}.\n"
        f"If you believe this is a mistake, please contact the organizer "
        f"or reply to this email with your transaction reference.\n\n"
        f"Order total: {order.total} {order.currency}\n"
    )
    from django.core.mail import send_mail
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[order.email],
        fail_silently=False,
    )