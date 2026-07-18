import base64
import hashlib
import hmac
import json
import os

import pytest
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment

from .conftest import make_payment

WEBHOOK_URL = "/eupago/webhook/"
API_KEY = "test-api-key"
WEBHOOK_SECRET = "test-webhook-secret"
ENCRYPTION_SECRET = "a" * 32  # AES-256 needs a 32-byte key


def make_confirmable_payment(order, provider, **kwargs):
    """Create a payment and stamp its info with the identifier the webhook handler expects."""
    payment = make_payment(order, provider, **kwargs)
    with scopes_disabled():
        info = json.loads(payment.info) if payment.info else {}
        info["identifier"] = f"{order.code}-{payment.pk}"
        payment.info = json.dumps(info)
        payment.save(update_fields=["info"])
    return payment


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def encrypt(plaintext: bytes, secret: str = ENCRYPTION_SECRET) -> tuple[str, str]:
    """Mirror euPago's webhook v2.0 "encrypt=true" mode: AES-256-CBC, PKCS7-padded."""
    iv = os.urandom(16)
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(secret.encode()), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()


@pytest.fixture(autouse=True)
def eupago_api_key(event):
    with scopes_disabled():
        event.settings.set("eupago_api_key", API_KEY)


@pytest.mark.django_db
def test_get_webhook_confirms_pending_payment(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    response = client.get(
        WEBHOOK_URL,
        {
            "identificador": f"{order.code}-{payment.pk}",
            "mp": "PC:PT",
            "chave_api": API_KEY,
        },
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
def test_get_webhook_wrong_chave_api_is_rejected(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    response = client.get(
        WEBHOOK_URL,
        {
            "identificador": f"{order.code}-{payment.pk}",
            "chave_api": "wrong-key",
        },
    )

    assert response.status_code == 400
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_get_webhook_missing_chave_api_is_rejected(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    response = client.get(WEBHOOK_URL, {"identificador": f"{order.code}-{payment.pk}"})

    assert response.status_code == 400
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


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
def test_post_webhook_refund_status_creates_external_refund(client, order):
    payment = make_confirmable_payment(
        order, "eupago_multibanco", state=OrderPayment.PAYMENT_STATE_CONFIRMED
    )

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps(
            {
                "transactions": {
                    "status": "Refund",
                    "identifier": f"{order.code}-{payment.pk}",
                    "method": "PC:PT",
                }
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    with scopes_disabled():
        assert payment.refunds.count() == 1
        refund = payment.refunds.get()
        assert refund.amount == payment.amount


@pytest.mark.django_db
def test_post_webhook_refund_status_ignored_for_unpaid_payment(client, order):
    payment = make_confirmable_payment(order, "eupago_multibanco")

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps(
            {
                "transactions": {
                    "status": "Refund",
                    "identifier": f"{order.code}-{payment.pk}",
                }
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    with scopes_disabled():
        assert payment.refunds.count() == 0


@pytest.mark.django_db
def test_post_webhook_requires_signature_when_secret_configured(client, order, event):
    with scopes_disabled():
        event.settings.set("eupago_webhook_secret", WEBHOOK_SECRET)
    payment = make_confirmable_payment(order, "eupago_mbway")
    body = json.dumps(
        {
            "transactions": {
                "status": "Paid",
                "identifier": f"{order.code}-{payment.pk}",
            }
        }
    ).encode()

    response = client.post(WEBHOOK_URL, data=body, content_type="application/json")

    assert response.status_code == 400
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_post_webhook_invalid_signature_is_rejected(client, order, event):
    with scopes_disabled():
        event.settings.set("eupago_webhook_secret", WEBHOOK_SECRET)
    payment = make_confirmable_payment(order, "eupago_mbway")
    body = json.dumps(
        {
            "transactions": {
                "status": "Paid",
                "identifier": f"{order.code}-{payment.pk}",
            }
        }
    ).encode()

    response = client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=sign(body, secret="wrong-secret"),
    )

    assert response.status_code == 400
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_post_webhook_valid_signature_confirms_payment(client, order, event):
    with scopes_disabled():
        event.settings.set("eupago_webhook_secret", WEBHOOK_SECRET)
    payment = make_confirmable_payment(order, "eupago_mbway")
    body = json.dumps(
        {
            "transactions": {
                "status": "Paid",
                "identifier": f"{order.code}-{payment.pk}",
            }
        }
    ).encode()

    response = client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=sign(body),
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_post_webhook_encrypted_payload_confirms_payment(client, order, event):
    with scopes_disabled():
        event.settings.set("eupago_webhook_secret", ENCRYPTION_SECRET)
    payment = make_confirmable_payment(order, "eupago_mbway")
    plaintext = json.dumps(
        {
            "transactions": {
                "status": "Paid",
                "identifier": f"{order.code}-{payment.pk}",
            }
        }
    ).encode()
    ciphertext_b64, iv_b64 = encrypt(plaintext)

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps({"data": ciphertext_b64}),
        content_type="application/json",
        HTTP_X_INITIALIZATION_VECTOR=iv_b64,
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_post_webhook_encrypted_payload_wrong_secret_is_ignored(client, order, event):
    with scopes_disabled():
        event.settings.set("eupago_webhook_secret", ENCRYPTION_SECRET)
    payment = make_confirmable_payment(order, "eupago_mbway")
    plaintext = json.dumps(
        {
            "transactions": {
                "status": "Paid",
                "identifier": f"{order.code}-{payment.pk}",
            }
        }
    ).encode()
    ciphertext_b64, iv_b64 = encrypt(plaintext, secret="x" * 32)

    response = client.post(
        WEBHOOK_URL,
        data=json.dumps({"data": ciphertext_b64}),
        content_type="application/json",
        HTTP_X_INITIALIZATION_VECTOR=iv_b64,
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


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

    response = client.get(
        WEBHOOK_URL,
        {"identificador": f"{order.code}-{payment.pk}", "chave_api": API_KEY},
    )

    assert response.status_code == 200
    with scopes_disabled():
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
