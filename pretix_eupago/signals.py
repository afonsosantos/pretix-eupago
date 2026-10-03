from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import register_payment_providers
from pretix.control.signals import nav_event, nav_event_settings


@receiver(register_payment_providers, dispatch_uid="payment_eupago")
def register_payment_provider(sender, **kwargs):
    from .payment import EupagoMBWAY, EupagoMultibanco

    return [EupagoMultibanco, EupagoMBWAY]


def _nav_entry(request, url_name, label, icon=None):
    resolved = request.resolver_match
    entry = {
        "label": label,
        "url": reverse(
            f"plugins:pretix_eupago:{url_name}",
            kwargs={
                "event": request.event.slug,
                "organizer": request.organizer.slug,
            },
        ),
        "active": resolved
        and resolved.namespace == "plugins:pretix_eupago"
        and resolved.url_name == url_name,
    }
    if icon:
        entry["icon"] = icon
    return entry


@receiver(nav_event, dispatch_uid="payment_eupago_nav")
def control_nav_orders(sender, request=None, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_view_orders", request=request
    ):
        return []
    return [_nav_entry(request, "orders", _("euPago"), icon="credit-card")]


@receiver(nav_event_settings, dispatch_uid="payment_eupago_nav_settings")
def control_nav_settings(sender, request=None, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings", request=request
    ):
        return []
    return [_nav_entry(request, "settings", _("euPago"))]
