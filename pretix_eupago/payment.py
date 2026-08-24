import json
import logging
from collections import OrderedDict
from datetime import timedelta

import requests
from django import forms
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException

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
        return ('<p>{} <a href="{}">{}</a></p>').format(
            _("The API key and sandbox mode are configured on the"),
            url,
            _("general euPago settings page"),
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
                            "How many days the Multibanco reference remains valid."
                        ),
                    ),
                ),
            ]
        )
        return OrderedDict(
            list(super().settings_form_fields.items()) + list(fields.items())
        )

    # No checkout form fields needed — Multibanco references are generated server-side.
    @property
    def payment_form_fields(self):
        return OrderedDict()

    def payment_is_valid_session(self, request):
        return True

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_eupago/checkout_multibanco.html")
        return template.render({"request": request})

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        expiry_days = self.settings.get(
            "multibanco_expiry_days", as_type=int, default=MULTIBANCO_EXPIRY_DAYS
        )
        expiry = (timezone.now().date() + timedelta(days=expiry_days)).strftime(
            "%Y-%m-%d"
        )
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
        except requests.RequestException:
            logger.exception("euPago Multibanco: HTTP error for payment %s", payment.pk)
            raise PaymentException(
                _("Could not reach the payment provider. Please try again later.")
            )

        if str(data.get("estado")) != "0":
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
        payment.order.send_mail(
            subject=_("Payment details for your order: {code}").format(
                code=payment.order.code
            ),
            template="pretix_eupago/mail_multibanco.txt",
            context={"info": info, "payment": payment},
        )

    def payment_pending_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/pending_multibanco.html")
        return template.render({"info": payment.info_data, "payment": payment})

    def payment_control_render(self, request, payment) -> str:
        template = get_template("pretix_eupago/control_multibanco.html")
        return template.render({"info": payment.info_data, "payment": payment})

    def order_pending_mail_render(self, order, payment) -> str:
        info = payment.info_data or {}
        if not info.get("referencia"):
            return ""
        template = get_template("pretix_eupago/mail_multibanco.txt")
        return template.render({"info": info, "payment": payment})

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
            payment.info = json.dumps({"phone": form.cleaned_data["phone"]})
            payment.save(update_fields=["info"])
            return True
        return False

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        # Phone may come from payment.info (payment_prepare flow) or session (checkout flow).
        info = payment.info_data or {}
        phone = info.get("phone") or request.session.get(
            f"payment_{self.identifier}_phone", ""
        )

        if not phone:
            raise PaymentException(_("No MB WAY phone number was provided."))

        # Normalise: strip whitespace, leading +351 or 351, then prefix "351#"
        phone = phone.strip().replace(" ", "")
        for prefix in ("+351", "351"):
            if phone.startswith(prefix):
                phone = phone[len(prefix) :]
                break
        phone = "351#" + phone

        identifier = f"{payment.order.code}-{payment.pk}"

        payload = {
            "payment": {
                "amount": {"value": float(payment.amount), "currency": "EUR"},
                "identifier": identifier,
            },
            "customer": {"phone": phone},
        }

        try:
            resp = requests.post(
                MBWAY_URL[self._env()],
                json=payload,
                timeout=30,
                headers={"Authorization": f"ApiKey {self._api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            logger.exception("euPago MBWAY: HTTP error for payment %s", payment.pk)
            raise PaymentException(
                _("Could not reach the payment provider. Please try again later.")
            )

        payment.info = json.dumps(
            {
                "phone": phone,
                "transactionID": data.get("transactionID", ""),
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
