from django.utils.translation import gettext_lazy as _

from pretix.base.plugins import PluginConfig

from . import __version__


class PluginApp(PluginConfig):
    name = "pretix_eupago"
    verbose_name = "euPago"
    default = True

    class PretixPluginMeta:
        name = _("euPago Payments")
        author = "Afonso Santos"
        description = _("Accept Multibanco and MB WAY payments via euPago")
        visible = True
        experimental = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=4.0.0"

    def ready(self):
        from . import signals  # NOQA
