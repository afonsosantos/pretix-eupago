import base64
import binascii
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
    eupago_webhook_secret = forms.CharField(
        label=_("Webhook signature secret"),
        required=False,
        help_text=_(
            "Optional but strongly recommended. The encryption key generated for this "
            "channel's webhook in Backoffice → Channels → Channel Listing → Receive "
            "notification for a URL. When set, incoming webhook v2.0 (POST) "
            "notifications are only accepted if their X-Signature header matches — "
            "without it, notifications are accepted based on a weaker cross-check only."
        ),
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
      v1.0 — GET request with query-string parameters. euPago includes the API key used to
             create the reference (``chave_api``) in every v1.0 notification; since that key
             is a secret only the event admin and euPago know, we require it to match the
             event's configured ``eupago_api_key`` before acting on the notification.
      v2.0 — POST request with a JSON body and an ``X-Signature`` header carrying an
             HMAC-SHA256 signature of the raw body, keyed with a secret generated per-channel
             in euPago's Backoffice. If the event has that secret configured
             (``eupago_webhook_secret``), the signature is verified before acting; a channel
             without a configured secret falls back to the weaker identifier cross-check only.

    See https://eupago.readme.io/reference/webhooks (v1.0) and
    https://eupago.readme.io/reference/realtime-webhooks-20 (v2.0).

    Configure one URL per euPago channel in Backoffice → Channels → Channel Listing
    → "Receive notification for a URL":
        https://<your-pretix-domain>/eupago/webhook/
    """

    def get(self, request, *args, **kwargs):
        """Webhook v1.0: payment data arrives as query-string parameters. Only sent when a
        reference has been paid — euPago does not send v1.0 notifications for expired,
        canceled, or refunded references."""
        params = request.GET
        identifier = params.get("identificador", "")
        mp = params.get("mp", "")  # PC:PT = Multibanco, MW:PT = MBWAY
        api_key = params.get("chave_api", "")

        if not identifier:
            logger.warning("euPago webhook v1.0: missing 'identificador' param")
            return HttpResponseBadRequest("missing identifier")

        logger.info("euPago webhook v1.0: mp=%s identifier=%s", mp, identifier)

        payment = self._lookup_payment(identifier)
        if payment is None:
            return HttpResponse("OK", content_type="text/plain")

        configured_key = payment.order.event.settings.get("eupago_api_key", default="")
        if not configured_key or not hmac.compare_digest(api_key, configured_key):
            logger.warning(
                "euPago webhook v1.0: chave_api mismatch for payment %s", payment.pk
            )
            return HttpResponseBadRequest("invalid credentials")

        self._confirm_payment(payment)
        return HttpResponse("OK", content_type="text/plain")

    def post(self, request, *args, **kwargs):
        """Webhook v2.0: JSON body, statuses Paid / Refund / Error / Cancel / Expired."""
        body = request.body
        try:
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

        if status not in ("Paid", "Refund", "Refunded"):
            return HttpResponse("OK", content_type="text/plain")

        if not identifier:
            logger.warning("euPago webhook v2.0: missing identifier in payload")
            return HttpResponseBadRequest("missing identifier")

        payment = self._lookup_payment(identifier)
        if payment is None:
            return HttpResponse("OK", content_type="text/plain")

        secret = payment.order.event.settings.get("eupago_webhook_secret", default="")
        if secret:
            signature = request.headers.get("X-Signature", "")
            if not _verify_hmac(body, signature, secret):
                logger.warning(
                    "euPago webhook v2.0: invalid signature for payment %s", payment.pk
                )
                return HttpResponseBadRequest("invalid signature")
        else:
            logger.warning(
                "euPago webhook v2.0: no webhook secret configured for event %s; "
                "skipping signature verification (identifier cross-check only)",
                payment.order.event.slug,
            )

        if status == "Paid":
            self._confirm_payment(payment)
        else:
            self._refund_payment(payment)

        return HttpResponse("OK", content_type="text/plain")

    def _lookup_payment(self, identifier: str):
        """
        Locate an OrderPayment by our stored identifier (format: ORDERCODE-PK) and verify it
        matches the identifier execute_payment() stamped onto payment.info. This guards
        against a customer replaying/forging their own (or a guessed) identifier — the
        caller must also check chave_api / X-Signature before acting on the result.
        """
        try:
            # identifier format: "ORDERCODE-PK", e.g. "SPMC2024-42"
            # rsplit to handle order codes that might contain hyphens
            parts = identifier.rsplit("-", 1)
            if len(parts) != 2:
                logger.warning(
                    "euPago webhook: unexpected identifier format: %s", identifier
                )
                return None

            payment_pk = int(parts[1])
            payment = OrderPayment.objects.select_related("order", "order__event").get(
                pk=payment_pk
            )

        except (ValueError, OrderPayment.DoesNotExist):
            logger.warning(
                "euPago webhook: payment not found for identifier=%s", identifier
            )
            return None
        except Exception:
            logger.exception(
                "euPago webhook: error looking up identifier=%s", identifier
            )
            return None

        stored = (payment.info_data or {}).get("identifier", "")
        if stored != identifier:
            logger.warning(
                "euPago webhook: identifier mismatch for payment %s (stored=%s received=%s)",
                payment_pk,
                stored,
                identifier,
            )
            return None

        return payment

    def _confirm_payment(self, payment: OrderPayment):
        """Confirm a pending payment. No-op if already in a terminal state."""
        if payment.state in (
            OrderPayment.PAYMENT_STATE_CONFIRMED,
            OrderPayment.PAYMENT_STATE_CANCELED,
            OrderPayment.PAYMENT_STATE_REFUNDED,
        ):
            logger.info(
                "euPago webhook: payment %s already in terminal state %s, skipping",
                payment.pk,
                payment.state,
            )
            return

        try:
            payment.confirm()
            logger.info(
                "euPago webhook: confirmed payment %s for order %s",
                payment.pk,
                payment.order.code,
            )
        except Exception:
            logger.exception("euPago webhook: error confirming payment %s", payment.pk)

    def _refund_payment(self, payment: OrderPayment):
        """
        Record a refund that was initiated externally (e.g. via euPago's Backoffice) so
        pretix's ledger reflects it. euPago's webhook payload does not carry a per-refund
        amount or id, so this is only sent for confirmed payments and is only acted on once
        per payment (a duplicate notification, e.g. from euPago's retry policy, is a no-op).
        """
        if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.info(
                "euPago webhook: ignoring refund notification for payment %s in state %s",
                payment.pk,
                payment.state,
            )
            return

        if payment.refunds.exists():
            logger.info(
                "euPago webhook: refund already recorded for payment %s, skipping",
                payment.pk,
            )
            return

        payment.create_external_refund()
        logger.info(
            "euPago webhook: recorded external refund for payment %s", payment.pk
        )


def _verify_hmac(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify euPago webhook v2.0's X-Signature header: HMAC-SHA256 of the raw request body,
    keyed with the channel's webhook secret, base64-encoded
    (see https://eupago.readme.io/reference/realtime-webhooks-20).
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    try:
        received = base64.b64decode(signature)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(expected, received)
