from django.urls import path

from .views import EupagoWebhookView

# Global URL patterns (not event-scoped).
# Pretix includes these at / so the final URL is:
#   https://<domain>/eupago/webhook/
urlpatterns = [
    path("eupago/webhook/", EupagoWebhookView.as_view(), name="eupago-webhook"),
]
