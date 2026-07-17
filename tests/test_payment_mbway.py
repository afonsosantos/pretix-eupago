import json

import pytest
import responses
from django.test import RequestFactory

from pretix.base.payment import PaymentException
from pretix_eupago.payment import MBWAY_URL, EupagoMBWAY

from .conftest import make_payment


@pytest.fixture
def provider(event):
    event.settings.set("eupago_api_key", "test-api-key")
    event.settings.set("eupago_sandbox", True)
    return EupagoMBWAY(event)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "raw_phone,expected",
    [
        ("912345678", "351#912345678"),
        ("351912345678", "351#912345678"),
        ("+351912345678", "351#912345678"),
        (" 912 345 678 ", "351#912345678"),
    ],
)
@responses.activate
def test_execute_payment_normalizes_phone(provider, order, raw_phone, expected):
    payment = make_payment(order, provider.identifier, info=json.dumps({"phone": raw_phone}))
    responses.add(
        responses.POST,
        MBWAY_URL["sandbox"],
        json={"transactionID": "abc-123"},
        status=200,
    )

    provider.execute_payment(RequestFactory().post("/"), payment)

    assert json.loads(responses.calls[0].request.body)["customer"]["phone"] == expected
    info = json.loads(payment.info)
    assert info["phone"] == expected
    assert info["transactionID"] == "abc-123"
    assert info["identifier"] == f"{order.code}-{payment.pk}"


@pytest.mark.django_db
def test_execute_payment_without_phone_raises(provider, order):
    payment = make_payment(order, provider.identifier, info=json.dumps({}))
    request = RequestFactory().post("/")
    request.session = {}

    with pytest.raises(PaymentException):
        provider.execute_payment(request, payment)


@pytest.mark.django_db
@responses.activate
def test_execute_payment_falls_back_to_session_phone(provider, order):
    payment = make_payment(order, provider.identifier, info=json.dumps({}))
    responses.add(
        responses.POST,
        MBWAY_URL["sandbox"],
        json={"transactionID": "abc-123"},
        status=200,
    )

    request = RequestFactory().post("/")
    request.session = {f"payment_{provider.identifier}_phone": "912345678"}

    provider.execute_payment(request, payment)

    info = json.loads(payment.info)
    assert info["phone"] == "351#912345678"


@pytest.mark.django_db
@responses.activate
def test_execute_payment_http_error_raises(provider, order):
    payment = make_payment(order, provider.identifier, info=json.dumps({"phone": "912345678"}))
    responses.add(responses.POST, MBWAY_URL["sandbox"], json={"error": "server error"}, status=500)

    with pytest.raises(PaymentException):
        provider.execute_payment(RequestFactory().post("/"), payment)


@pytest.mark.django_db
def test_payment_prepare_stores_phone(provider, order):
    payment = make_payment(order, provider.identifier)
    request = RequestFactory().post(
        "/",
        data={"payment": provider.identifier, "payment_eupago_mbway-phone": "912345678"},
    )
    request.session = {}

    assert provider.payment_prepare(request, payment) is True
    assert json.loads(payment.info) == {"phone": "912345678"}


@pytest.mark.django_db
def test_payment_prepare_invalid_form_returns_false(provider, order):
    payment = make_payment(order, provider.identifier)
    request = RequestFactory().post("/", data={"payment": provider.identifier})
    request.session = {}

    assert provider.payment_prepare(request, payment) is False


@pytest.mark.django_db
def test_matching_id_and_api_payment_details(provider, order):
    payment = make_payment(order, provider.identifier, info=json.dumps({"transactionID": "abc-123"}))

    assert provider.matching_id(payment) == "abc-123"
    assert provider.api_payment_details(payment) == {"transaction_id": "abc-123"}


@pytest.mark.django_db
def test_shred_payment_info_redacts_phone(provider, order):
    payment = make_payment(
        order, provider.identifier, info=json.dumps({"phone": "351#912345678", "transactionID": "abc-123"})
    )

    provider.shred_payment_info(payment)

    info = json.loads(payment.info)
    assert info["phone"] == "*** redacted ***"
    assert info["transactionID"] == "abc-123"
