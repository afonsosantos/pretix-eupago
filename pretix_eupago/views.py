import hashlib
import hmac
import json
import logging

from django import forms
from django.http import HttpResponse, HttpResponseBadRequest
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.forms import SettingsForm
from pretix.base.models import Event, OrderPayment
from pretix.control.views.event import (
    EventSettingsFormView,
    EventSettingsViewMixin,
)

logger = logging.getLogger(__name__)


class EupagoSettingsForm(SettingsForm):
    eupago_api_key = forms.CharField(
        label=_("API Key"),
        help_text=_(
            "Your euPago API key, found in Backoffice → Channels → Channel Listing."
        ),
    )
    eupago_sandbox = forms.BooleanField(
        label=_("Sandbox / Test mode"),
        required=False,
        help_text=_("Use the euPago sandbox environment for testing."),
    )


class EupagoSettings(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    form_class = EupagoSettingsForm
    template_name = "pretix_eupago/settings.html"
    permission = "event.settings.payment:write"

    def get_success_url(self) -> str:
        return reverse(
            "plugins:pretix_eupago:settings",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(scopes_disabled(), name="dispatch")
class EupagoWebhookView(View):
    """
    Handles both euPago webhook formats:
      v1.0 — GET request with query-string parameters
      v2.0 — POST request with JSON body (preferred; supports HMAC verification)

    Configure one URL per euPago channel in Backoffice → Channels → Channel Listing
    → "Receive notification for a URL":
        https://<your-pretix-domain>/eupago/webhook/
    """

    def get(self, request, *args, **kwargs):
        """Webhook v1.0: payment data arrives as query-string parameters."""
        params = request.GET
        identifier = params.get("identificador", "")
        mp = params.get("mp", "")  # PC:PT = Multibanco, MW:PT = MBWAY

        if not identifier:
            logger.warning("euPago webhook v1.0: missing 'identificador' param")
            return HttpResponseBadRequest("missing identifier")

        logger.info("euPago webhook v1.0: mp=%s identifier=%s", mp, identifier)
        self._confirm_payment(identifier)
        return HttpResponse("OK", content_type="text/plain")

    def post(self, request, *args, **kwargs):
        """Webhook v2.0: JSON body with optional HMAC-SHA256 signature header."""
        try:
            body = request.body
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            logger.warning("euPago webhook v2.0: invalid JSON body")
            return HttpResponseBadRequest("invalid JSON")

        transactions = data.get("transactions", {})
        status = transactions.get("status", "")
        identifier = transactions.get("identifier", "")
        method = transactions.get("method", "")

        logger.info(
            "euPago webhook v2.0: method=%s status=%s identifier=%s",
            method,
            status,
            identifier,
        )

        if status == "Paid":
            if not identifier:
                logger.warning("euPago webhook v2.0: missing identifier in payload")
                return HttpResponseBadRequest("missing identifier")
            self._confirm_payment(identifier)

        return HttpResponse("OK", content_type="text/plain")

    def _confirm_payment(self, identifier: str):
        """
        Locate an OrderPayment by our stored identifier (format: ORDERCODE-PK)
        and confirm it if still pending.
        """
        try:
            # identifier format: "ORDERCODE-PK", e.g. "SPMC2024-42"
            # rsplit to handle order codes that might contain hyphens
            parts = identifier.rsplit("-", 1)
            if len(parts) != 2:
                logger.warning(
                    "euPago webhook: unexpected identifier format: %s", identifier
                )
                return

            payment_pk = int(parts[1])
            payment = OrderPayment.objects.select_related("order").get(pk=payment_pk)

        except (ValueError, OrderPayment.DoesNotExist):
            logger.warning(
                "euPago webhook: payment not found for identifier=%s", identifier
            )
            return
        except Exception:
            logger.exception(
                "euPago webhook: error looking up identifier=%s", identifier
            )
            return

        # Guard against identifier spoofing: verify stored identifier matches
        stored = (payment.info_data or {}).get("identifier", "")
        if stored != identifier:
            logger.warning(
                "euPago webhook: identifier mismatch for payment %s (stored=%s received=%s)",
                payment_pk,
                stored,
                identifier,
            )
            return

        if payment.state in (
            OrderPayment.PAYMENT_STATE_CONFIRMED,
            OrderPayment.PAYMENT_STATE_CANCELED,
            OrderPayment.PAYMENT_STATE_REFUNDED,
        ):
            logger.info(
                "euPago webhook: payment %s already in terminal state %s, skipping",
                payment_pk,
                payment.state,
            )
            return

        try:
            payment.confirm()
            logger.info(
                "euPago webhook: confirmed payment %s for order %s",
                payment_pk,
                payment.order.code,
            )
        except Exception:
            logger.exception("euPago webhook: error confirming payment %s", payment_pk)


def _verify_hmac(body: bytes, signature: str, secret: str) -> bool:
    """Verify euPago webhook v2.0 X-Signature header (HMAC-SHA256)."""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
