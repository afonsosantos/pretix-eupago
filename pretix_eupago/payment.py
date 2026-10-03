import json
import logging
import re
from collections import OrderedDict
from datetime import date, timedelta

import requests
from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import eventreverse_absolute

logger = logging.getLogger(__name__)

MULTIBANCO_URL = {
    "production": "https://clientes.eupago.pt/clientes/rest_api/multibanco/create",
    "sandbox": "https://sandbox.eupago.pt/clientes/rest_api/multibanco/create",
}
MBWAY_URL = {
    "production": "https://clientes.eupago.pt/api/v1.02/mbway/create",
    "sandbox": "https://sandbox.eupago.pt/api/v1.02/mbway/create",
}

# Default Multibanco reference validity in days
MULTIBANCO_EXPIRY_DAYS = 7


def normalize_phone(value):
    """
    The 9-digit national number euPago's `customerPhone` expects, or None if ``value``
    isn't a Portuguese mobile number. Accepts any spacing/punctuation and a +351, 00351 or
    351 prefix; a foreign number is rejected rather than sent with a hard-coded +351.
    """
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00351"):
        digits = digits[5:]
    elif digits.startswith("351") and len(digits) == 12:
        digits = digits[3:]
    return digits if re.fullmatch(r"9\d{8}", digits) else None


def validate_phone(value):
    if normalize_phone(value) is None:
        raise ValidationError(
            _(
                "Enter a valid Portuguese mobile number (9 digits, starting with 9), "
                "e.g. 912 345 678."
            )
        )


def _expiry_date(info):
    # Stored as an ISO string in payment.info; parsed for locale-aware display.
    try:
        return date.fromisoformat(info.get("expiry", ""))
    except (TypeError, ValueError):
        return None


class EupagoSettingsMixin:
    """
    API key and sandbox mode are shared by every euPago payment method, so they live on the
    plugin's own settings page (``EupagoSettings``/``eupago_api_key``/``eupago_sandbox`` on
    ``event.settings``) rather than being duplicated in each provider's settings_form_fields.
    """

    @property
    def _is_sandbox(self):
        return self.event.settings.get("eupago_sandbox", as_type=bool, default=False)

    @property
    def _api_key(self):
        return self.event.settings.get("eupago_api_key", default="")

    def _env(self):
        return "sandbox" if self._is_sandbox else "production"

    def is_allowed(self, request, total=None):
        # euPago only charges in euros. And sandbox payments are fake: offering them on a
        # live (non-test-mode) shop would hand out real tickets for no money.
        if self.event.currency != "EUR":
            return False
        if self._is_sandbox and not self.event.testmode:
            return False
        return super().is_allowed(request, total)

    @property
    def test_mode_message(self):
        if self._is_sandbox:
            return _(
                "euPago sandbox mode is active. No real payments will be processed."
            )
        return None

    def settings_content_render(self, request) -> str:
        url = reverse(
            "plugins:pretix_eupago:settings",
            kwargs={
                "organizer": self.event.organizer.slug,
                "event": self.event.slug,
            },
        )
        # One translatable sentence with the link markers inside, so translators can move
        # the link wherever their word order needs it.
        sentence = str(
            _(
                "The API key, sandbox mode and webhook key are configured on the "
                "{link}general euPago settings page{end}."
            )
        )
        return format_html(
            "<p>{}</p>",
            mark_safe(
                conditional_escape(sentence).format(
                    link=format_html('<a href="{}">', url), end="</a>"
                )
            ),
        )


class EupagoMultibanco(EupagoSettingsMixin, BasePaymentProvider):
    identifier = "eupago_multibanco"
    verbose_name = _("Multibanco (euPago)")
    public_name = _("Multibanco")

    @property
    def settings_form_fields(self):
        fields = OrderedDict(
            [
                (
                    "multibanco_expiry_days",
                    forms.IntegerField(
                        label=_("Multibanco reference validity (days)"),
                        min_value=1,
                        max_value=365,
                        initial=MULTIBANCO_EXPIRY_DAYS,
                        required=False,
                        help_text=_(
                            "How many days the Multibanco reference remains valid. "
                            "Capped at the order's payment deadline."
                        ),
                    ),
                ),
            ]
        )
        return OrderedDict(
            list(super().settings_form_fields.items()) + list(fields.items())
        )

    @property
    def _expiry_days(self):
        return self.settings.get(
            "multibanco_expiry_days", as_type=int, default=MULTIBANCO_EXPIRY_DAYS
        )

    # No checkout form fields needed — Multibanco references are generated server-side.
    @property
    def payment_form_fields(self):
        return OrderedDict()

    def payment_is_valid_session(self, request):
        return True

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_eupago/checkout_multibanco.html")
        return template.render({"request": request, "days": self._expiry_days})

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        tz = self.event.timezone
        today = now().astimezone(tz).date()
        # Never outlive the order: a reference still payable after the order expired (and
        # its quota was resold) takes the customer's money for tickets that are gone.
        order_deadline = payment.order.expires.astimezone(tz).date()
        expiry = max(
            today, min(today + timedelta(days=self._expiry_days), order_deadline)
        ).isoformat()
        identifier = f"{payment.order.code}-{payment.pk}"

        payload = {
            "chave": self._api_key,
            "valor": float(payment.amount),
            "id": identifier,
            "per_dup": 0,
            "data_fim": expiry,
        }

        try:
            resp = requests.post(
                MULTIBANCO_URL[self._env()],
                json=payload,
                timeout=30,
                headers={"ApiKey": self._api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            logger.exception("euPago Multibanco: HTTP error for payment %s", payment.pk)
            raise PaymentException(
                _("Could not reach the payment provider. Please try again later.")
            )

        if (
            not isinstance(data, dict)
            or str(data.get("estado")) != "0"
            or not data.get("referencia")
            or not data.get("entidade")
        ):
            logger.error(
                "euPago Multibanco: API error for payment %s: %s",
                payment.pk,
                data,
            )
            raise PaymentException(
                _(
                    "The payment provider returned an error. Please try a different payment method."
                )
            )

        info = {
            "referencia": data["referencia"],
            "entidade": data["entidade"],
            "valor": str(data.get("valor", payment.amount)),
            "identifier": identifier,
            "expiry": expiry,
        }
        payment.info = json.dumps(info)
        payment.state = OrderPayment.PAYMENT_STATE_PENDING
        payment.save(update_fields=["info", "state"])
        # Payment is pending — confirmed via webhook when customer pays.

        # The "order placed" email is sent by pretix core before execute_payment runs
        # (it can't know the reference yet), so {payment_info} is empty there. Send the
        # reference separately once we actually have it.
        order = payment.order
        order.send_mail(
            # Left lazy and unformatted: pretix formats it with the context below inside
            # the order's language, so the subject isn't stuck in the request's language.
            subject=_("Multibanco payment details for {event} – order {code}"),
            template="pretix_eupago/mail_multibanco.txt",
            context={
                **self._render_context(payment),
                "event": order.event,
                "code": order.code,
                "order_url": eventreverse_absolute(
                    order.event,
                    "presale:event.order.open",
                    kwargs={
                        "order": order.code,
                        "secret": order.secret,
                        "hash": order.email_confirm_secret(),
                    },
                ),
            },
        )

    def _render_context(self, payment):
        info = payment.info_data or {}
        return {"info": info, "payment": payment, "expiry": _expiry_date(info)}

    def payment_pending_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/pending_multibanco.html")
        return template.render(self._render_context(payment))

    def payment_control_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/control_multibanco.html")
        return template.render(self._render_context(payment))

    def order_pending_mail_render(self, order, payment) -> str:
        info = payment.info_data or {}
        if not info.get("referencia"):
            return ""
        template = get_template("pretix_eupago/mail_multibanco.txt")
        return template.render(self._render_context(payment))

    def shred_payment_info(self, obj):
        # Multibanco references are not personal data; nothing to shred.
        pass

    def api_payment_details(self, payment):
        info = payment.info_data or {}
        return {
            "entidade": info.get("entidade"),
            "referencia": info.get("referencia"),
            "expiry": info.get("expiry"),
        }

    def matching_id(self, payment):
        info = payment.info_data or {}
        return info.get("referencia")


class EupagoMBWAY(EupagoSettingsMixin, BasePaymentProvider):
    identifier = "eupago_mbway"
    verbose_name = _("MB WAY (euPago)")
    public_name = _("MB WAY")

    @property
    def payment_form_fields(self):
        return OrderedDict(
            [
                (
                    "phone",
                    forms.CharField(
                        label=_("MB WAY phone number"),
                        help_text=_(
                            "Portuguese mobile number registered with MB WAY (e.g. 912 345 678). "
                            "You have 5 minutes to approve the payment in the MB WAY app."
                        ),
                        max_length=20,
                        validators=[validate_phone],
                        widget=forms.TextInput(
                            attrs={
                                "type": "tel",
                                "inputmode": "tel",
                                "autocomplete": "tel-national",
                                "placeholder": "912 345 678",
                            }
                        ),
                    ),
                )
            ]
        )

    def payment_is_valid_session(self, request):
        return bool(request.session.get(f"payment_{self.identifier}_phone"))

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_eupago/checkout_mbway.html")
        return template.render(
            {
                "phone": request.session.get(f"payment_{self.identifier}_phone", ""),
                "request": request,
            }
        )

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment):
        """Called when adding/retrying payment on the order detail page."""
        form = self.payment_form(request)
        if form.is_valid():
            payment.info = json.dumps(
                {"phone": normalize_phone(form.cleaned_data["phone"])}
            )
            payment.save(update_fields=["info"])
            return True
        return False

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        # Phone may come from payment.info (payment_prepare flow) or session (checkout flow).
        info = payment.info_data or {}
        raw_phone = info.get("phone") or request.session.get(
            f"payment_{self.identifier}_phone", ""
        )

        if not raw_phone:
            raise PaymentException(_("No MB WAY phone number was provided."))

        phone = normalize_phone(raw_phone)
        if phone is None:
            raise PaymentException(
                _(
                    "Enter a valid Portuguese mobile number (9 digits, starting with 9), "
                    "e.g. 912 345 678."
                )
            )

        identifier = f"{payment.order.code}-{payment.pk}"

        payload = {
            "payment": {
                "amount": {"value": float(payment.amount), "currency": "EUR"},
                "identifier": identifier,
                "customerPhone": phone,
                "countryCode": "+351",
            },
        }

        # No raise_for_status(): euPago answers a refused request (e.g. a number not
        # registered with MB WAY) with a 4xx *and* a JSON body saying so, and that has to
        # reach the customer as "check the number", not "try again later".
        try:
            resp = requests.post(
                MBWAY_URL[self._env()],
                json=payload,
                timeout=30,
                headers={"Authorization": f"ApiKey {self._api_key}"},
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            logger.exception("euPago MBWAY: HTTP error for payment %s", payment.pk)
            raise PaymentException(
                _("Could not reach the payment provider. Please try again later.")
            )

        if (
            not isinstance(data, dict)
            or data.get("transactionStatus") != "Success"
            or not data.get("transactionID")
        ):
            logger.error(
                "euPago MBWAY: request refused for payment %s (HTTP %s): %s",
                payment.pk,
                resp.status_code,
                data,
            )
            raise PaymentException(
                _(
                    "We couldn't send the MB WAY request to this number. Check that it "
                    "is registered with MB WAY, or choose another payment method."
                )
            )

        payment.info = json.dumps(
            {
                "phone": phone,
                "transactionID": data["transactionID"],
                "identifier": identifier,
            }
        )
        payment.state = OrderPayment.PAYMENT_STATE_PENDING
        payment.save(update_fields=["info", "state"])
        # Payment is pending — confirmed via webhook when customer approves in app.

    def payment_pending_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/pending_mbway.html")
        return template.render({"info": payment.info_data, "payment": payment})

    def payment_control_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/control_mbway.html")
        return template.render({"info": payment.info_data, "payment": payment})

    def shred_payment_info(self, obj):
        d = obj.info_data or {}
        d["phone"] = "*** redacted ***"
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])

    def api_payment_details(self, payment):
        info = payment.info_data or {}
        return {"transaction_id": info.get("transactionID")}

    def matching_id(self, payment):
        info = payment.info_data or {}
        return info.get("transactionID")
