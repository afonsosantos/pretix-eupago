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

This is a plugin for `pretix`_ that integrates `euPago`_, a Portuguese payment
gateway, to accept **Multibanco** references and **MB WAY** push payments.

* PyPI: https://pypi.org/project/pretix-eupago/
* Source: https://github.com/afonsosantos/pretix-eupago
* Issues: https://github.com/afonsosantos/pretix-eupago/issues

Installation
------------

Install the plugin into the same Python environment as your pretix instance::

    pip install pretix-eupago

Then restart your pretix server. The plugin registers itself with pretix's plugin registry
automatically via a setuptools entry point — no changes to ``INSTALLED_APPS`` are needed. You can now
enable it for an event under the "plugins" tab in that event's settings.

Development setup
------------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``pip install -e .`` (or, if you use `uv`_, ``uv sync --extra test``) within this directory
   to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by
   enabling it in the 'plugins' tab in the settings.

Run ``make test`` to run the test suite and ``make lint`` to run `ruff`_.

Configuration
-------------

Enable the plugin for an event, then configure the shared euPago settings under
**Settings → Payment → euPago**:

* **API Key** — your euPago API key, found in Backoffice → Channels → Channel Listing.
* **Sandbox / Test mode** — use the euPago sandbox environment for testing.

Both are shared by every euPago payment method, so you only set them once. Then enable and configure
each payment method you want individually under Settings → Payment:

* **Multibanco** — has no method-specific settings besides the reference validity:

  * **Multibanco reference validity (days)** — how many days a generated Multibanco reference stays
    valid.

* **MB WAY** — no method-specific settings.

Finally, configure a single webhook URL in euPago's Backoffice (Channels → Channel Listing → "Receive
notification for a URL"), for every channel you use::

    https://<your-pretix-domain>/eupago/webhook/

This URL is global (not event-scoped) and handles both euPago webhook formats (v1.0 GET and v2.0 POST).

License
-------

Copyright 2026 Afonso Santos

Released under the terms of the Apache License 2.0, see the LICENSE file for details.

.. _pretix: https://github.com/pretix/pretix
.. _euPago: https://eupago.pt
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
.. _uv: https://docs.astral.sh/uv/
.. _ruff: https://docs.astral.sh/ruff/
