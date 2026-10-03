import json
from datetime import timedelta

import pytest
import responses
from django.core import mail
from django.test import RequestFactory
from django.utils import timezone, translation
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.base.payment import PaymentException

from pretix_eupago.payment import MULTIBANCO_URL, EupagoMultibanco

from .conftest import make_payment


@pytest.fixture
def provider(event):
    event.settings.set("eupago_api_key", "test-api-key")
    event.settings.set("eupago_sandbox", True)
    return EupagoMultibanco(event)


@pytest.mark.django_db
@responses.activate
def test_execute_payment_success(provider, order):
    payment = make_payment(order, provider.identifier)
    responses.add(
        responses.POST,
        MULTIBANCO_URL["sandbox"],
        json={
            "estado": 0,
            "referencia": "123456789",
            "entidade": "12345",
            "valor": "23.00",
        },
        status=200,
    )

    provider.execute_payment(RequestFactory().post("/"), payment)

    info = json.loads(payment.info)
    assert info["referencia"] == "123456789"
    assert info["entidade"] == "12345"
    assert info["identifier"] == f"{order.code}-{payment.pk}"
    assert info["expiry"] == (timezone.now().date() + timedelta(days=7)).strftime(
        "%Y-%m-%d"
    )
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    assert len(mail.outbox) == 1
    assert "123 456 789" in mail.outbox[0].body
    assert "12345" in mail.outbox[0].body


SUCCESS = {
    "estado": 0,
    "referencia": "123456789",
    "entidade": "12345",
    "valor": "23.00",
}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "days,expected_days",
    [
        (3, 3),
        # The order (conftest) expires in 10 days: the reference mustn't outlive it.
        (14, 10),
    ],
)
@responses.activate
def test_execute_payment_expiry_is_configured_days_capped_at_order_deadline(
    provider, order, days, expected_days
):
    provider.settings.set("multibanco_expiry_days", days)
    payment = make_payment(order, provider.identifier)
    responses.add(responses.POST, MULTIBANCO_URL["sandbox"], json=SUCCESS, status=200)

    provider.execute_payment(RequestFactory().post("/"), payment)

    info = json.loads(payment.info)
    sent = json.loads(responses.calls[0].request.body)
    expected = (timezone.now().date() + timedelta(days=expected_days)).isoformat()
    assert info["expiry"] == expected
    assert sent["data_fim"] == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        {"estado": 0, "entidade": "12345"},  # no referencia
        {"estado": 0, "referencia": "123456789"},  # no entidade
        [1, 2, 3],  # not an object
    ],
)
@responses.activate
def test_execute_payment_malformed_response_raises(provider, order, body):
    payment = make_payment(order, provider.identifier)
    responses.add(responses.POST, MULTIBANCO_URL["sandbox"], json=body, status=200)

    with pytest.raises(PaymentException):
        provider.execute_payment(RequestFactory().post("/"), payment)


@pytest.mark.django_db
@responses.activate
def test_execute_payment_non_json_response_raises(provider, order):
    payment = make_payment(order, provider.identifier)
    responses.add(responses.POST, MULTIBANCO_URL["sandbox"], body="<html>", status=200)

    with pytest.raises(PaymentException):
        provider.execute_payment(RequestFactory().post("/"), payment)


@pytest.mark.django_db
@responses.activate
def test_execute_payment_email_is_in_order_language(provider, order, event):
    with scopes_disabled():
        event.settings.set("locales", ["en", "pt-pt"])
        order.locale = "pt-pt"
        order.save()
    payment = make_payment(order, provider.identifier)
    responses.add(responses.POST, MULTIBANCO_URL["sandbox"], json=SUCCESS, status=200)

    provider.execute_payment(RequestFactory().post("/"), payment)

    message = mail.outbox[0]
    assert "Dummy" in message.subject
    assert order.code in message.subject
    assert "Multibanco" in message.subject
    assert "Referência" in message.body
    assert f"/order/{order.code}/{order.secret}/" in message.body


@pytest.mark.django_db
@responses.activate
def test_execute_payment_api_error_raises(provider, order):
    payment = make_payment(order, provider.identifier)
    responses.add(
        responses.POST,
        MULTIBANCO_URL["sandbox"],
        json={"estado": -1, "resposta": "invalid key"},
        status=200,
    )

    with pytest.raises(PaymentException):
        provider.execute_payment(RequestFactory().post("/"), payment)


@pytest.mark.django_db
@responses.activate
def test_execute_payment_http_error_raises(provider, order):
    payment = make_payment(order, provider.identifier)
    responses.add(
        responses.POST,
        MULTIBANCO_URL["sandbox"],
        json={"error": "server error"},
        status=500,
    )

    with pytest.raises(PaymentException):
        provider.execute_payment(RequestFactory().post("/"), payment)


@pytest.mark.django_db
def test_matching_id_and_api_payment_details(provider, order):
    payment = make_payment(
        order,
        provider.identifier,
        info=json.dumps(
            {"referencia": "123456789", "entidade": "12345", "expiry": "2026-01-01"}
        ),
    )

    assert provider.matching_id(payment) == "123456789"
    assert provider.api_payment_details(payment) == {
        "entidade": "12345",
        "referencia": "123456789",
        "expiry": "2026-01-01",
    }


@pytest.mark.django_db
def test_pending_render_shows_formatted_details(provider, order):
    payment = make_payment(
        order,
        provider.identifier,
        info=json.dumps(
            {"referencia": "123456789", "entidade": "12345", "expiry": "2026-10-10"}
        ),
    )

    html = provider.payment_pending_render(None, payment)

    assert "123 456 789" in html
    assert "12345" in html
    assert "€23.00" in html
    assert "Pay by" in html

    # A locale date, not the raw ISO string stored in payment.info (pretix's English
    # short format happens to *be* ISO, hence another language here).
    with translation.override("de"):
        html = provider.payment_pending_render(None, payment)
    assert "10.10.2026" in html


@pytest.mark.django_db
def test_checkout_confirm_render_states_validity_days(provider):
    provider.settings.set("multibanco_expiry_days", 3)
    assert "3 days" in provider.checkout_confirm_render(None)


@pytest.mark.django_db
def test_is_allowed_only_for_eur(provider, event):
    request = RequestFactory().get("/")
    with scopes_disabled():
        event.testmode = True
        event.currency = "USD"
        assert provider.is_allowed(request) is False


@pytest.mark.django_db
def test_is_allowed_refuses_sandbox_on_live_event(provider, event):
    # The provider fixture turns sandbox on: fake payments must not confirm real orders.
    request = RequestFactory().get("/")
    request.session = {}
    with scopes_disabled():
        event.testmode = False
        assert provider.is_allowed(request) is False


@pytest.mark.django_db
def test_shred_payment_info_is_noop(provider, order):
    payment = make_payment(
        order,
        provider.identifier,
        info=json.dumps({"referencia": "123456789", "entidade": "12345"}),
    )

    provider.shred_payment_info(payment)

    assert json.loads(payment.info) == {"referencia": "123456789", "entidade": "12345"}
