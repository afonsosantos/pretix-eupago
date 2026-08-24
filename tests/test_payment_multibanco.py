import json
from datetime import timedelta

import pytest
import responses
from django.core import mail
from django.test import RequestFactory
from django.utils import timezone
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


@pytest.mark.django_db
@responses.activate
def test_execute_payment_uses_configured_expiry(provider, order):
    provider.settings.set("multibanco_expiry_days", 14)
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
    assert info["expiry"] == (timezone.now().date() + timedelta(days=14)).strftime(
        "%Y-%m-%d"
    )


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
def test_shred_payment_info_is_noop(provider, order):
    payment = make_payment(
        order,
        provider.identifier,
        info=json.dumps({"referencia": "123456789", "entidade": "12345"}),
    )

    provider.shred_payment_info(payment)

    assert json.loads(payment.info) == {"referencia": "123456789", "entidade": "12345"}
