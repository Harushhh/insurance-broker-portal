"""Background e-mail sending (callers run it in a thread)."""
from django.core.mail import send_mail
from django.conf import settings
import logging


logger = logging.getLogger(__name__)


# -------------------------
# EMAIL BACKGROUND
# -------------------------
def send_email_background(subject, message, recipient_list):
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        logger.info("Background email sent to %d recipient(s)", len(recipient_list))
    except Exception:
        logger.exception("Background email failed")
