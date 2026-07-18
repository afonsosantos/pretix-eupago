# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pretix-eupago` is a standalone, pip-installable [pretix](https://github.com/pretix/pretix) plugin
(published on PyPI as `pretix-eupago`, importable as `pretix_eupago`) that integrates
[euPago](https://eupago.pt), a Portuguese payment gateway, to accept **Multibanco** references and
**MB WAY** push payments. It is *not* part of the pretix monorepo — it's discovered by a pretix instance
at runtime via a setuptools entry point, the mechanism pretix uses for all external (non-core) plugins.

## Architecture

- `pretix_eupago/__init__.py` — declares `__version__` and re-exports `PluginApp` from `apps.py`. Also
  fails fast with a clear error if `pretix` itself isn't importable (guards against `pip install`
  being run outside a pretix environment).
- `pretix_eupago/apps.py` — `PluginApp` (subclasses `pretix.base.plugins.PluginConfig`, not plain
  Django `AppConfig`) declares the `PretixPluginMeta` (name, category `PAYMENT`, `experimental = True`,
  `compatibility = "pretix>=4.0.0"`) and hooks up `signals.py` in `ready()`.
  - **This file must stay a real submodule** (`pretix_eupago/apps.py`), not be inlined into
    `__init__.py`. pretix's plugin loader (`pretix/settings.py`) reads only the *module* portion of the
    `pretix.plugin` entry point (`pretix_eupago:PluginApp` → module `pretix_eupago`) and appends that
    bare string to `INSTALLED_APPS`. Django's app-config auto-discovery for a bare app name only looks
    inside an `apps` submodule of that package for a single `AppConfig` subclass — it does **not**
    search `__init__.py`. If `apps.py` were removed or renamed, the plugin would silently load as a
    generic `AppConfig` with no `PretixPluginMeta`, and pretix would not register it as a payment
    provider.
- `pretix_eupago/signals.py` — connects to pretix's `register_payment_providers` signal to expose the
  two payment provider classes to pretix core.
- `pretix_eupago/payment.py` — the core logic, two `BasePaymentProvider` subclasses sharing
  `EupagoSettingsMixin`:
  - `EupagoMultibanco` — no checkout form fields, but has its own `settings_form_fields` override for
    `multibanco_expiry_days` (provider-specific, read via `self.settings`, pretix's per-provider
    `SettingsSandbox`). `execute_payment` calls euPago's `multibanco/create` REST API and stores
    `referencia`/`entidade`/expiry in `payment.info` (JSON). Payment stays pending until the webhook
    confirms it.
  - `EupagoMBWAY` — checkout form collects a phone number (also has a `payment_prepare` path for the
    "retry payment" flow on the order page); `execute_payment` normalizes the phone to euPago's
    `351#XXXXXXXXX` format and calls `mbway/create`. Also pending until webhook confirmation.
  - Both providers implement `payment_pending_render` / `payment_control_render` (per-provider
    templates), `api_payment_details`, `matching_id`, and `shred_payment_info` (MB WAY redacts the
    phone number; Multibanco has nothing to shred).
  - Neither provider implements `execute_refund` — refunds are not currently supported and must be
    handled manually/out-of-band.
- `EupagoSettingsMixin` (top of `payment.py`) holds only what's genuinely shared: `_api_key`,
  `_is_sandbox`, `_env()`, `test_mode_message`, and `settings_content_render` (a link back to the
  shared settings page, shown on each provider's own settings screen so admins don't go looking for a
  field that isn't there anymore). It deliberately does **not** override `settings_form_fields` — see
  below for why the API key/sandbox fields live elsewhere.
- `pretix_eupago/views.py` also defines `EupagoSettingsForm` (a `pretix.base.forms.SettingsForm`) and
  `EupagoSettings` (`EventSettingsViewMixin` + `EventSettingsFormView`) — a single Control-panel page,
  shared across both payment methods, holding `eupago_api_key` and `eupago_sandbox`. These are read
  directly off `event.settings` (not a provider's `SettingsSandbox`) by `EupagoSettingsMixin`, since
  `SettingsSandbox` always prefixes keys per-provider (`payment_<identifier>_...`) and there is no
  built-in way to share one field across providers other than storing it unprefixed on `event.settings`
  and having each provider read it directly. `permission = "event.settings.payment:write"`.
  - `pretix_eupago/urls.py` registers this at a literal `control/event/<organizer>/<event>/eupago/settings`
    path (not `event_patterns` — those are for the presale/frontend side; Control-panel plugin pages use
    a plain `urlpatterns` entry with the full path spelled out, following the same pattern as pretix's
    own `returnurl`/`badges` plugins) and `apps.py`'s `PretixPluginMeta.settings_links` points the event
    settings nav at it (`plugins:pretix_eupago:settings`).
  - `pretix_eupago/templates/pretix_eupago/settings.html` extends
    `pretixcontrol/event/settings_base.html`, the standard shell for this kind of page.
- `pretix_eupago/views.py` — `EupagoWebhookView`, a CSRF-exempt Django view handling **both** euPago
  webhook formats:
  - v1.0: `GET` with query-string params (`identificador`, `mp`)
  - v2.0: `POST` with a JSON body (`transactions.status/identifier/method`)
  Both paths funnel into `_confirm_payment(identifier)`, which parses the identifier as
  `"{order.code}-{payment.pk}"` (via `rsplit("-", 1)`), looks up the `OrderPayment`, cross-checks the
  identifier stored in `payment.info_data` (spoofing guard), skips payments already in a terminal
  state, and calls `payment.confirm()`.
  - Note: `_verify_hmac` (HMAC-SHA256 signature check for webhook v2.0) is defined at module level but
    is **not currently called** anywhere in `post()` — signature verification is not enforced yet.
  - The whole view is decorated with `@method_decorator(scopes_disabled(), name="dispatch")`
    (`django_scopes`). This is required, not optional: `/eupago/webhook/` is a global URL with no
    `organizer`/`event` in its path, so pretix's usual scope-activation (which scopes queries by the
    matched URL kwargs) never runs for this view. Without `scopes_disabled()`, any `OrderPayment`
    query raises `django_scopes.exceptions.ScopeError`, which the broad `except Exception` in
    `_confirm_payment` silently swallows — the webhook returns `200 OK` but never actually confirms
    the payment. Sibling in-tree plugins with global webhook endpoints (`stripe`, `paypal2`) use the
    same guard for the same reason.
- `pretix_eupago/urls.py` — registers a **global** (non-event-scoped) URL, `/eupago/webhook/`, for the
  webhook endpoint; this is the single URL configured in euPago's Backoffice for every channel.
- `pretix_eupago/templates/pretix_eupago/` — checkout confirmation, pending-payment, control
  (admin/backoffice), and order-pending-email templates, one pair per payment method
  (`_multibanco` / `_mbway`).
- No Django models and no migrations — nothing here needs `manage.py migrate`.

### Payment identifier convention

Every payment sent to euPago is tagged with `identifier = f"{payment.order.code}-{payment.pk}"`. This
identifier is both sent to euPago as the transaction id and stored back into `payment.info_data`. The
webhook handler relies on this exact format (and the stored copy) to locate and authenticate the
payment being confirmed — keep both sides in sync if this format ever changes.

### Displaying amounts

Templates must not hand-format `payment.amount` or echo amount fields returned by euPago's API (e.g.
`valor`) directly — those can carry the wrong number of decimal places. Use pretix's standard
`{% load money %}` / `{{ payment.amount|money:payment.order.event.currency }}` filter
(`pretix.base.templatetags.money`), the same convention used by pretix's own `stripe` and
`banktransfer` plugins, which formats to the currency's correct precision and locale.

### Sandbox vs. production

`EupagoSettingsMixin._env()` picks between the `sandbox` and `production` entries in `MULTIBANCO_URL` /
`MBWAY_URL` based on the shared `event.settings.eupago_sandbox` value. There's no shared euPago API
client — each provider makes its own `requests.post(...)` calls inline in `execute_payment`.

### Shared vs. per-provider settings — which goes where

When adding a new setting, decide: does every euPago payment method need this value, or just one? If
shared (like the API key), add a form field to `EupagoSettingsForm` in `views.py` prefixed `eupago_`
and read it in `EupagoSettingsMixin` via `self.event.settings.get(...)`. If provider-specific (like
`multibanco_expiry_days`), add it to that provider's own `settings_form_fields` override and read it
via `self.settings.get(...)` (the provider's `SettingsSandbox`) — don't put it in the mixin, since the
mixin is shared code and its `settings_form_fields` (if it had one) would apply to every provider that
uses it.

## Plugin registration mechanics (external vs. in-tree plugins)

Unlike plugins built into the pretix monorepo (which are listed directly in `INSTALLED_APPS`), this
plugin is discovered via the `pretix.plugin` setuptools entry point declared in `pyproject.toml`:

```toml
[project.entry-points."pretix.plugin"]
pretix_eupago = "pretix_eupago:PluginApp"
```

`pretix/settings.py` iterates `importlib.metadata.entry_points(group='pretix.plugin')` and appends the
*module* part of each entry point value to `INSTALLED_APPS`. See "Architecture" above for why this
constrains `apps.py` to remain a real submodule.

## Tests

```bash
make install               # uv sync --extra test — pulls in real pretix core, not just this plugin
make test                  # uv run pytest — runs the whole suite
uv run pytest tests/test_webhook.py::test_get_webhook_confirms_pending_payment -v   # single test
make lint                  # uv run ruff check . && uv run ruff format --check .
make format                # uv run ruff check --fix . && uv run ruff format .
```

Dependency and environment management uses [uv](https://docs.astral.sh/uv/); `uv.lock` is the
committed lockfile. `ruff` (lint + format) is declared under `[dependency-groups].dev` in
`pyproject.toml`, not `test`, since it's a contributor tool rather than something the test suite
imports.

- `tests/settings.py` is just `from pretix.testutils.settings import *` — that module (shipped inside
  the `pretix` package itself) builds a full, working Django settings module on top of `pretix.settings`
  (sqlite by default, Celery eager, dummy cache, migrations disabled *unless* the `GITHUB_WORKFLOW` env
  var is set, in which case real migrations run — that's inherited pretix CI behavior, not something
  this repo controls). `DJANGO_SETTINGS_MODULE` is pointed at it via `[tool.pytest.ini_options]` in
  `pyproject.toml`.
- `pyproject.toml` also sets `pythonpath = ["."]` under `[tool.pytest.ini_options]`. This isn't optional
  boilerplate: `pytest-django` imports `DJANGO_SETTINGS_MODULE` ("tests.settings") in a hook that runs
  *before* pytest's normal conftest-driven `sys.path` setup. Running the bare `pytest` console script
  (as CI does) — as opposed to `python -m pytest`, which adds the cwd to `sys.path` itself — doesn't put
  the repo root on `sys.path` in time, so `tests.settings` fails to import. `pythonpath = ["."]` makes
  pytest insert the rootdir regardless of invocation style. If CI ever fails with
  `ImportError: No module named 'tests.settings'`, this is why.
- Because the plugin is installed (even in `-e` editable mode) into the same environment pytest runs
  in, its `pretix.plugin` entry point is picked up automatically — no manual `INSTALLED_APPS` wiring
  needed in test settings.
- `tests/conftest.py` provides `organizer`/`event`/`order` fixtures and a `make_payment()` helper. Model
  creation must happen inside `with scopes_disabled():` (`django_scopes`) since fixtures run without an
  active organizer/event scope.
- HTTP calls to euPago (`requests.post` in `payment.py`) are mocked with `responses`
  (`@responses.activate` + `responses.add(...)`), matching the pattern used in pretix's own test suite
  (e.g. `tests/base/test_webhooks.py`) rather than `unittest.mock`.
- `RequestFactory` requests passed into `execute_payment`/`payment_prepare` don't have a `.session` by
  default — set `request.session = {}` (or a dict with the expected keys) before calling into MB WAY's
  provider methods, since `EupagoMBWAY.execute_payment` falls back to reading
  `request.session["payment_eupago_mbway_phone"]` when `payment.info` has no phone.
- `tests/test_settings.py` exercises the shared settings page (`EupagoSettings`) through the real
  Control-panel URL with the Django test `client`, not just the form/view classes directly — including
  a permission check. That means it needs a logged-in user with access to the event: create a `User`,
  a `Team` with `all_event_permissions=True` limited to that event via `team.limit_events.add(event)`,
  then `client.login(email=..., password=...)` (works because `pretix.testutils.settings` sets
  `PRETIX_AUTH_BACKENDS = ['pretix.base.auth.NativeAuthBackend']`). Accessing an event settings page
  without permission returns `404`, not `403` — matches pretix's own convention of not revealing that a
  resource exists to users without access.

## Commands

To manually exercise changes against a real pretix instance (beyond what the test suite covers):

```bash
# From a pretix dev checkout with its virtualenv activated:
pip install -e /path/to/pretix-eupago
cd <pretix>/src
python manage.py check     # validates the app loads and PretixPluginMeta is picked up
python manage.py runserver
```

Then enable "euPago Payments" under an event's Settings → Plugins tab, set the API key and sandbox
toggle under Settings → Payment → euPago (the shared settings page), and enable/configure the
Multibanco/MB WAY providers individually under Settings → Payment.

Build distribution artifacts locally:

```bash
uv build                       # produces dist/*.tar.gz and dist/*.whl
uvx twine check dist/*
```

## CI / Publishing

`.github/workflows/ci.yml` has four jobs, all using `astral-sh/setup-uv`:

- `lint` / `test` — run on every push to `main` and on every GitHub Release. `lint` runs
  `ruff check` and `ruff format --check`; `test` runs `uv sync --extra test` then `uv run pytest`.
- `build` / `publish` — only run for the `release` event (`if: github.event_name == 'release'`), and
  `needs: [lint, test]`, so a release only reaches PyPI if both lint and the test suite pass.
  `build` runs `uv build`. Publishing uses PyPI **trusted publishing** (OIDC) — no API token is
  stored as a repo secret. The `publish` job's `environment: pypi` must match the environment name
  registered as the trusted publisher on PyPI's project settings
  (https://pypi.org/manage/account/publishing/).

Bump the version in both `pyproject.toml` and `pretix_eupago/__init__.py` before cutting a release —
they are not currently kept in sync automatically.
