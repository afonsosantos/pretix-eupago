from django.urls import re_path

from .views import EupagoOrdersView, EupagoSettings, EupagoWebhookView

# Plain (non-namespaced-by-event) URL patterns. Pretix includes these as-is, so the
# webhook ends up at:
#   https://<domain>/eupago/webhook/
# and the settings/orders pages' full paths are spelled out explicitly, ending up at:
#   https://<domain>/control/event/<organizer>/<event>/eupago/settings
#   https://<domain>/control/event/<organizer>/<event>/eupago/orders
urlpatterns = [
    # The trailing slash is optional: euPago's POSTs are not redirected by Django's
    # APPEND_SLASH, so a webhook URL saved in the Backoffice without it would 404 forever.
    re_path(r"^eupago/webhook/$", EupagoWebhookView.as_view(), name="eupago-webhook"),
    # Unnamed alias without the slash, so reverse() (used by the settings page) keeps
    # producing the canonical URL with it.
    re_path(r"^eupago/webhook$", EupagoWebhookView.as_view()),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/eupago/settings$",
        EupagoSettings.as_view(),
        name="settings",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/eupago/orders$",
        EupagoOrdersView.as_view(),
        name="orders",
    ),
]
