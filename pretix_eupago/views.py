import base64
import binascii
import hashlib
import hmac
import json
import logging
from urllib.parse import urljoin

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django import forms
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView
from django_scopes import scopes_disabled
from pretix.base.forms import SecretKeySettingsField, SettingsForm
from pretix.base.models import Event, Event_SettingsStore, OrderPayment, Quota
from pretix.base.services.locking import LockTimeoutException
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import (
    EventSettingsFormView,
    EventSettingsViewMixin,
)

EUPAGO_PROVIDERS = ("eupago_multibanco", "eupago_mbway")

PAID_STATUSES = ("Paid",)
REFUND_STATUSES = ("Refund", "Refunded")
# "Error" too: an MB WAY push that failed would otherwise sit pending forever.
FAIL_STATUSES = ("Cancel", "Canceled", "Expired", "Error")

logger = logging.getLogger(__name__)


class PluginEnabledMixin:
    """
    404 unless the plugin is enabled for the event. pretix only checks that for a
    plugin's event_patterns; these Control-panel URLs are plain urlpatterns.
    """

    def dispatch(self, request, *args, **kwargs):
        if "pretix_eupago" not in request.event.get_plugins():
            raise Http404()
        return super().dispatch(request, *args, **kwargs)


def _no_autofill(form):
    """
    Keep browsers and password managers out of the settings inputs: they'd fill the
    admin's pretix login into the API key. The data-* attributes are the opt-outs
    Bitwarden, 1Password, LastPass and Dashlane each honour.
    """
    for field in form.fields.values():
        field.widget.attrs.update(
            {
                "autocomplete": "off",
                "data-bwignore": "true",
                "data-1p-ignore": "true",
                "data-lpignore": "true",
                "data-form-type": "other",
            }
        )


class EupagoSettingsForm(SettingsForm):
    eupago_api_key = SecretKeySettingsField(
        label=_("API key"),
        help_text=_(
            "Your euPago API key, found in Backoffice → Channels → Channel Listing."
        ),
    )
    eupago_sandbox = forms.BooleanField(
        label=_("Sandbox mode"),
        required=False,
        help_text=_(
            "Use euPago's sandbox for testing. Requires an API key from "
            "sandbox.eupago.pt — your production key will not work. Payment methods "
            "are only offered while the event is in test mode."
        ),
    )
    eupago_webhook_secret = SecretKeySettingsField(
        label=_("Webhook key"),
        required=False,
        help_text=_(
            "The key euPago generates for this channel's webhook (Backoffice → Channels "
            "→ Channel Listing → Receive notification for a URL). Required for webhook "
            "2.0: it lets pretix verify that notifications really come from euPago, and "
            "decrypt them if you enable webhook encryption in euPago."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _no_autofill(self)


class EupagoSettings(EventSettingsViewMixin, PluginEnabledMixin, EventSettingsFormView):
    model = Event
    form_class = EupagoSettingsForm
    template_name = "pretix_eupago/settings.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        ctx["webhook_url"] = urljoin(
            settings.SITE_URL, reverse("plugins:pretix_eupago:eupago-webhook")
        )
        ctx["missing_webhook_secret"] = not event.settings.get(
            "eupago_webhook_secret", default=""
        )
        ctx["sandbox_on_live_event"] = (
            event.settings.get("eupago_sandbox", as_type=bool, default=False)
            and not event.testmode
        )
        ctx["wrong_currency"] = event.currency != "EUR"
        return ctx

    def get_success_url(self) -> str:
        return reverse(
            "plugins:pretix_eupago:settings",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class EupagoOrdersView(EventPermissionRequiredMixin, PluginEnabledMixin, ListView):
    """
    Lists every OrderPayment made with a euPago provider for this event, along with the
    method-specific data (Multibanco entidade/referencia, MB WAY transactionID) stashed in
    payment.info by execute_payment — the same data the order detail page and the webhook
    handler read from.
    """

    template_name = "pretix_eupago/orders.html"
    permission = "can_view_orders"
    context_object_name = "payments"
    paginate_by = 50

    def get_queryset(self):
        qs = (
            OrderPayment.objects.filter(
                order__event=self.request.event, provider__in=EUPAGO_PROVIDERS
            )
            .select_related("order")
            .order_by("-created")
        )

        provider = self.request.GET.get("provider")
        if provider in EUPAGO_PROVIDERS:
            qs = qs.filter(provider=provider)

        state = self.request.GET.get("state")
        if state in dict(OrderPayment.PAYMENT_STATES):
            qs = qs.filter(state=state)

        # Order code or a Multibanco reference / MB WAY transaction ID — what an admin
        # has in hand when a customer calls. References are shown grouped "123 456 789",
        # so spaces are dropped before matching against payment.info.
        query = self.request.GET.get("query", "").strip()
        if query:
            qs = qs.filter(
                Q(order__code__icontains=query)
                | Q(info__icontains=query.replace(" ", ""))
            )

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["provider_choices"] = (
            ("eupago_multibanco", _("Multibanco")),
            ("eupago_mbway", _("MB WAY")),
        )
        ctx["state_choices"] = OrderPayment.PAYMENT_STATES
        ctx["query"] = self.request.GET.get("query", "").strip()
        ctx["filtered"] = any(
            self.request.GET.get(k) for k in ("provider", "state", "query")
        )
        return ctx


def _ok():
    return HttpResponse("OK", content_type="text/plain")


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
             in euPago's Backoffice (``eupago_webhook_secret``). Without that secret a
             notification can't be told apart from one a customer forged for their own
             order, so Paid/Refund are refused; only Cancel/Expired/Error — which at worst
             fail a payment the customer can simply retry — fall back to the identifier
             cross-check. A channel with euPago's optional "encrypt" webhook setting
             enabled sends an AES-256-CBC-encrypted body instead (``{"data": ..., ...}``
             with no plaintext ``transaction``); see ``_decrypt_payload``. Note euPago's
             own docs call this field "transactions" (plural), but the real payload key is
             singular ``"transaction"``.

    See https://eupago.readme.io/reference/webhooks (v1.0) and
    https://eupago.readme.io/reference/realtime-webhooks-20 (v2.0).

    Configure one URL per euPago channel in Backoffice → Channels → Channel Listing
    → "Receive notification for a URL" (shown on the plugin's settings page):
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
            return _ok()

        configured_key = payment.order.event.settings.get("eupago_api_key", default="")
        if not configured_key or not hmac.compare_digest(
            api_key.encode(), configured_key.encode()
        ):
            # Same response as an unknown identifier, so the endpoint doesn't reveal
            # which identifiers exist.
            logger.warning(
                "euPago webhook v1.0: chave_api mismatch for payment %s", payment.pk
            )
            return _ok()

        return self._confirm_payment(payment)

    def post(self, request, *args, **kwargs):
        """Webhook v2.0: JSON body, statuses Paid / Refund / Error / Cancel / Expired."""
        body = request.body
        try:
            data = json.loads(body)
        except ValueError:
            logger.warning("euPago webhook v2.0: invalid JSON body")
            return HttpResponseBadRequest("invalid JSON")
        if not isinstance(data, dict):
            return HttpResponseBadRequest("invalid payload")

        decrypted_with = None
        if "data" in data and "transaction" not in data:
            # The channel has euPago's "encrypt" webhook option enabled: the whole body is
            # AES-256-CBC ciphertext, keyed with the same per-channel secret used for
            # X-Signature.
            iv = request.headers.get("X-Initialization-Vector", "")
            data, decrypted_with = self._decrypt_payload(data.get("data"), iv)
            if data is None:
                logger.warning(
                    "euPago webhook v2.0: could not decrypt payload with any configured "
                    "webhook secret"
                )
                return _ok()

        transaction = data.get("transaction")
        if not isinstance(transaction, dict):
            return HttpResponseBadRequest("invalid payload")
        status = transaction.get("status", "")
        identifier = transaction.get("identifier", "")

        logger.info(
            "euPago webhook v2.0: method=%s status=%s identifier=%s",
            transaction.get("method", ""),
            status,
            identifier,
        )

        if status not in PAID_STATUSES + REFUND_STATUSES + FAIL_STATUSES:
            return _ok()

        if not identifier or not isinstance(identifier, str):
            logger.warning("euPago webhook v2.0: missing identifier in payload")
            return HttpResponseBadRequest("missing identifier")

        payment = self._lookup_payment(identifier)
        if payment is None:
            return _ok()

        secret = payment.order.event.settings.get("eupago_webhook_secret", default="")
        if decrypted_with is not None:
            # Decrypting proves the sender knows *some* event's secret — it has to be the
            # one of the event this payment belongs to, or an admin of any event on this
            # instance could confirm payments in everyone else's.
            if not secret or not hmac.compare_digest(
                decrypted_with.encode(), secret.encode()
            ):
                logger.warning(
                    "euPago webhook v2.0: payload for payment %s was encrypted with "
                    "another event's webhook key",
                    payment.pk,
                )
                return HttpResponseBadRequest("invalid credentials")
        elif secret:
            signature = request.headers.get("X-Signature", "")
            if not _verify_hmac(body, signature, secret):
                logger.warning(
                    "euPago webhook v2.0: invalid signature for payment %s",
                    payment.pk,
                )
                return HttpResponseBadRequest("invalid signature")
        elif status not in FAIL_STATUSES:
            logger.error(
                "euPago webhook v2.0: no webhook key configured for event %s; refusing "
                "unverifiable %s notification for payment %s",
                payment.order.event.slug,
                status,
                payment.pk,
            )
            return HttpResponseBadRequest("webhook key not configured")

        if status in PAID_STATUSES:
            return self._confirm_payment(payment)
        if status in FAIL_STATUSES:
            self._fail_payment(payment, status)
            return _ok()
        return self._refund_payment(payment)

    def _decrypt_payload(self, ciphertext_b64, iv_b64):
        """
        Decrypt an encrypted webhook body, returning ``(payload, secret)`` or
        ``(None, None)``. This endpoint is shared by every event and nothing outside the
        ciphertext says which one it's for, so every configured
        ``eupago_webhook_secret`` is tried. A wrong key still passes PKCS7 unpadding about
        once in 256 tries, so a key only counts once its plaintext is also a JSON object
        with a ``transaction`` — otherwise the next key is tried, rather than the real
        notification being dropped. See https://eupago.readme.io/reference/realtime-webhooks-20.
        """
        if not isinstance(ciphertext_b64, str) or not ciphertext_b64 or not iv_b64:
            return None, None
        try:
            ciphertext = base64.b64decode(ciphertext_b64)
            iv = base64.b64decode(iv_b64)
        except (binascii.Error, ValueError):
            return None, None

        secrets = (
            Event_SettingsStore.objects.filter(key="eupago_webhook_secret")
            .exclude(value="")
            .values_list("value", flat=True)
            .distinct()
        )
        for secret in secrets:
            plaintext = _aes_cbc_decrypt(ciphertext, iv, secret.encode())
            if plaintext is None:
                continue
            try:
                payload = json.loads(plaintext)
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(
                payload.get("transaction"), dict
            ):
                return payload, secret
        return None, None

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
        """
        Confirm a payment euPago reports as paid. Canceled/failed ones too, like pretix's
        own stripe plugin: pretix cancels a pending Multibanco payment when the customer
        switches method, but the reference stays payable at euPago until it expires — and
        if they pay it anyway, the money has to show up in pretix.
        """
        if payment.state not in (
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CANCELED,
            OrderPayment.PAYMENT_STATE_FAILED,
        ):
            logger.info(
                "euPago webhook: payment %s already in state %s, skipping",
                payment.pk,
                payment.state,
            )
            return _ok()

        try:
            payment.confirm()
        except LockTimeoutException:
            # Non-200 so euPago retries instead of the payment being lost.
            return HttpResponse("Lock timeout, please try again.", status=503)
        except Quota.QuotaExceededException:
            # pretix records the payment but can't mark the order paid (e.g. it expired
            # and its tickets were resold). Retrying won't change that; an admin has to.
            logger.error(
                "euPago webhook: payment %s for order %s received, but quota is exceeded; "
                "the order needs manual attention",
                payment.pk,
                payment.order.code,
            )
            return _ok()
        logger.info(
            "euPago webhook: confirmed payment %s for order %s",
            payment.pk,
            payment.order.code,
        )
        return _ok()

    def _fail_payment(self, payment: OrderPayment, status: str):
        """
        Mark a pending payment as failed after euPago reports the customer canceled the
        MB WAY push, the push errored, or the reference expired. pretix's
        ``OrderPayment.fail()`` only acts on payments in created/pending/canceled state and
        is race-safe against a simultaneous confirmation, so a stray Cancel/Expired after a
        Paid is a no-op. No refund is involved — the payment never completed.
        """
        try:
            info = dict(payment.info_data or {})
            info["eupago_webhook_status"] = status
            payment.fail(info=info)
            logger.info(
                "euPago webhook: failed payment %s (status=%s)", payment.pk, status
            )
        except Exception:
            logger.exception("euPago webhook: error failing payment %s", payment.pk)

    def _refund_payment(self, payment: OrderPayment):
        """
        Record a refund that was initiated externally (e.g. via euPago's Backoffice) so
        pretix's ledger reflects it. euPago's webhook payload does not carry a per-refund
        amount or id, so this is only acted on once per payment (a duplicate notification,
        e.g. from euPago's retry policy, is a no-op). The row lock keeps two concurrent
        retries from both recording one.
        """
        with db_transaction.atomic():
            payment = OrderPayment.objects.select_for_update().get(pk=payment.pk)
            if payment.state in (
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
            ):
                # The Paid notification hasn't arrived yet. Non-200 makes euPago retry
                # this one later instead of the refund being dropped for good.
                logger.info(
                    "euPago webhook: refund for payment %s arrived before it was "
                    "confirmed; asking euPago to retry",
                    payment.pk,
                )
                return HttpResponse("payment not confirmed yet", status=409)
            if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                logger.info(
                    "euPago webhook: ignoring refund notification for payment %s in "
                    "state %s",
                    payment.pk,
                    payment.state,
                )
                return _ok()
            if payment.refunds.exists():
                logger.info(
                    "euPago webhook: refund already recorded for payment %s, skipping",
                    payment.pk,
                )
                return _ok()
            payment.create_external_refund()
        logger.info(
            "euPago webhook: recorded external refund for payment %s", payment.pk
        )
        return _ok()


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


def _aes_cbc_decrypt(ciphertext: bytes, iv: bytes, key: bytes):
    """
    Decrypt an euPago webhook v2.0 encrypted payload (AES-256-CBC, PKCS7-padded). Returns
    ``None`` on any error — a wrong key usually, though not always, fails PKCS7 unpadding
    (see ``_decrypt_payload``); a key of the wrong length fails here too.
    """
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError):
        return None
