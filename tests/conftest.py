from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import Event, Order, OrderPayment, Organizer


@pytest.fixture
def organizer():
    with scopes_disabled():
        return Organizer.objects.create(name="Dummy", slug="dummy")


@pytest.fixture
def event(organizer):
    with scopes_disabled():
        return Event.objects.create(
            organizer=organizer,
            name="Dummy",
            slug="dummy",
            date_from=now(),
            live=True,
            currency="EUR",
        )


@pytest.fixture
def order(event):
    with scopes_disabled():
        return Order.objects.create(
            code="FOOBAR",
            event=event,
            email="dummy@example.org",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("23.00"),
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )


def make_payment(order, provider, amount=None, info="{}", state=OrderPayment.PAYMENT_STATE_CREATED):
    with scopes_disabled():
        return order.payments.create(
            amount=amount if amount is not None else order.total,
            provider=provider,
            state=state,
            info=info,
        )
