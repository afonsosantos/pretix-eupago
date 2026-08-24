import json

import pytest
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, Team, User

from .conftest import make_payment

ORDERS_URL = "/control/event/{}/{}/eupago/orders"


@pytest.fixture
def logged_in_client(client, event):
    with scopes_disabled():
        user = User.objects.create_user("dummy@example.org", "dummy")
        team = Team.objects.create(
            organizer=event.organizer, all_event_permissions=True
        )
        team.members.add(user)
        team.limit_events.add(event)
    client.login(email="dummy@example.org", password="dummy")
    return client


@pytest.mark.django_db
def test_orders_page_lists_eupago_payments_only(logged_in_client, event, order):
    make_payment(
        order,
        "eupago_multibanco",
        info=json.dumps({"referencia": "123456789", "entidade": "12345"}),
        state=OrderPayment.PAYMENT_STATE_PENDING,
    )
    make_payment(
        order,
        "eupago_mbway",
        info=json.dumps({"transactionID": "abc-123"}),
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
    )
    make_payment(order, "banktransfer")

    response = logged_in_client.get(ORDERS_URL.format(event.organizer.slug, event.slug))

    assert response.status_code == 200
    assert [p.provider for p in response.context["payments"]] == [
        "eupago_mbway",
        "eupago_multibanco",
    ]
    assert b"12345" in response.content
    assert b"123456789" in response.content
    assert b"abc-123" in response.content


@pytest.mark.django_db
def test_orders_page_filters_by_provider(logged_in_client, event, order):
    make_payment(
        order,
        "eupago_multibanco",
        info=json.dumps({"referencia": "111111111", "entidade": "11111"}),
    )
    make_payment(
        order, "eupago_mbway", info=json.dumps({"transactionID": "mbway-only"})
    )

    response = logged_in_client.get(
        ORDERS_URL.format(event.organizer.slug, event.slug),
        {"provider": "eupago_mbway"},
    )

    assert response.status_code == 200
    assert b"mbway-only" in response.content
    assert b"111111111" not in response.content


@pytest.mark.django_db
def test_orders_page_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.get(ORDERS_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 404
