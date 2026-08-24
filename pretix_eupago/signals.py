from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import register_payment_providers
from pretix.control.signals import nav_event


@receiver(register_payment_providers, dispatch_uid="payment_eupago")
def register_payment_provider(sender, **kwargs):
    from .payment import EupagoMBWAY, EupagoMultibanco

    return [EupagoMultibanco, EupagoMBWAY]


@receiver(nav_event, dispatch_uid="payment_eupago_nav")
def control_nav_orders(sender, request=None, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "event.orders:read", request=request
    ):
        return []
    url = resolve(request.path_info)
    return [
        {
            "label": _("euPago orders"),
            "url": reverse(
                "plugins:pretix_eupago:orders",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.event.organizer.slug,
                },
            ),
            "icon": "credit-card",
            "active": url.namespace == "plugins:pretix_eupago"
            and url.url_name == "orders",
        },
    ]
