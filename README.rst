pretix-eupago
==============

.. image:: https://img.shields.io/pypi/v/pretix-eupago.svg
   :target: https://pypi.org/project/pretix-eupago/
   :alt: PyPI version

.. image:: https://img.shields.io/pypi/pyversions/pretix-eupago.svg
   :target: https://pypi.org/project/pretix-eupago/
   :alt: Supported Python versions

.. image:: https://img.shields.io/pypi/l/pretix-eupago.svg
   :target: https://github.com/afonsosantos/pretix-eupago/blob/main/LICENSE
   :alt: License

.. image:: https://github.com/afonsosantos/pretix-eupago/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/afonsosantos/pretix-eupago/actions/workflows/ci.yml
   :alt: CI status

.. image:: https://deepwiki.com/badge.svg
   :target: https://deepwiki.com/afonsosantos/pretix-eupago
   :alt: DeepWiki

This is a plugin for `pretix`_ that integrates `euPago`_, a Portuguese payment
gateway, to accept **Multibanco** references and **MB WAY** push payments.

**Documentation: https://docs.afonsosantos.me/eupago/** — configuration, webhooks, refunds and
troubleshooting, in English and Portuguese.

* PyPI: https://pypi.org/project/pretix-eupago/
* Source: https://github.com/afonsosantos/pretix-eupago
* Issues: https://github.com/afonsosantos/pretix-eupago/issues

Installation
------------

Install the plugin into the same Python environment as your pretix instance::

    pip install pretix-eupago

Restart pretix and enable it for an event under **Settings → Plugins**. Then follow the
`configuration guide`_ — in particular, payments are only confirmed once euPago's webhook is set up.

Development
-----------

With a working `pretix development setup`_ and its virtualenv active::

    pip install -e .    # or: uv sync --extra test
    make                # compile translations
    make test
    make lint

See the `development guide`_ for more.

License
-------

Copyright 2026 Afonso Santos

Released under the terms of the Apache License 2.0, see the LICENSE file for details.

.. _pretix: https://github.com/pretix/pretix
.. _euPago: https://eupago.pt
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
.. _configuration guide: https://docs.afonsosantos.me/eupago/configuration/
.. _development guide: https://docs.afonsosantos.me/development/
