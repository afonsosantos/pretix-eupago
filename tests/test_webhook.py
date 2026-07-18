import json

import pytest
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment

from .conftest import make_payment

WEBHOOK_URL = "/eupago/webhook/"


def make_confirmable_payment(order, provider, **kwargs):
    """Create a payment and stamp its info with the identifier the webhook handler expects."""
    payment = make_payment(order, provider, **kwargs)
    with scopes_disabled():
        info = json.loads(payment.info) if payment.info else {}
        info["identifier"] = f"{order.code}-{payment.pk}"
        payment.info = json.dumps(info)
        payment.save(update_fields=["info"])
    return payment


@pytest.mark.django_db
def test_get_webhook_confirms_pending_payment(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    response = client.get(
        WEBHOOK_URL, {"identificador": f"{order.code}-{payment.pk}", "mp": "PC:PT"}
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_get_webhook_missing_identifier_is_bad_request(client):
    response = client.get(WEBHOOK_URL)
    assert response.status_code == 400


@pytest.mark.django_db
def test_post_webhook_paid_confirms_payment(client, order):
    payment = make_confirmable_payment(order, "eupago_mbway")

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps(
            {
                "transactions": {
                    "status": "Paid",
                    "identifier": f"{order.code}-{payment.pk}",
                    "method": "MW:PT",
                }
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_post_webhook_non_paid_status_does_not_confirm(client, order):
    payment = make_confirmable_payment(order, "eupago_mbway")

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps(
            {
                "transactions": {
                    "status": "Pending",
                    "identifier": f"{order.code}-{payment.pk}",
                }
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_post_webhook_invalid_json_is_bad_request(client):
    response = client.post(
        WEBHOOK_URL, data="not json", content_type="application/json"
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_post_webhook_paid_without_identifier_is_bad_request(client):
    response = client.post(
        WEBHOOK_URL,
        data=json.dumps({"transactions": {"status": "Paid", "identifier": ""}}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_webhook_rejects_spoofed_identifier(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    # Attacker guesses a valid-looking identifier for someone else's payment PK, but the
    # stored identifier on that payment won't match this crafted string.
    forged_identifier = f"SOMEOTHERCODE-{payment.pk}"
    response = client.get(WEBHOOK_URL, {"identificador": forged_identifier})

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_webhook_unknown_payment_does_not_error(client):
    response = client.get(WEBHOOK_URL, {"identificador": "NOPE-999999"})
    assert response.status_code == 200


@pytest.mark.django_db
def test_webhook_malformed_identifier_does_not_error(client):
    response = client.get(
        WEBHOOK_URL, {"identificador": "not-a-valid-identifier-format-!!"}
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_webhook_already_confirmed_payment_is_left_alone(client, order):
    payment = make_confirmable_payment(
        order, "eupago_multibanco", state=OrderPayment.PAYMENT_STATE_CONFIRMED
    )

    response = client.get(WEBHOOK_URL, {"identificador": f"{order.code}-{payment.pk}"})

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
