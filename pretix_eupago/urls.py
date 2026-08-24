from django.urls import path, re_path

from .views import EupagoOrdersView, EupagoSettings, EupagoWebhookView

# Plain (non-namespaced-by-event) URL patterns. Pretix includes these as-is, so the
# webhook ends up at:
#   https://<domain>/eupago/webhook/
# and the settings/orders pages' full paths are spelled out explicitly, ending up at:
#   https://<domain>/control/event/<organizer>/<event>/eupago/settings
#   https://<domain>/control/event/<organizer>/<event>/eupago/orders
urlpatterns = [
    path("eupago/webhook/", EupagoWebhookView.as_view(), name="eupago-webhook"),
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
