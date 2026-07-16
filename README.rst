pretix-eupago
==============

This is a plugin for `pretix`_ that integrates `euPago`_, a Portuguese payment
gateway, to accept **Multibanco** references and **MB WAY** push payments.

Development setup
------------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``python setup.py develop`` (or ``pip install -e .``) within this directory to register this
   application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by
   enabling it in the 'plugins' tab in the settings.

Configuration
-------------

Enable the plugin for an event, then go to the event's payment settings and configure the desired
provider(s) (Multibanco and/or MB WAY):

* **API Key** — your euPago API key, found in Backoffice → Channels → Channel Listing.
* **Sandbox / Test mode** — use the euPago sandbox environment for testing.
* **Multibanco reference validity (days)** — how many days a generated Multibanco reference stays valid.

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
