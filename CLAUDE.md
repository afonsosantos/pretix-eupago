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
  Django `AppConfig`) declares the `PretixPluginMeta` (name, category `PAYMENT`, `experimental = False`,
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
    `referencia`/`entidade`/expiry in `payment.info` (JSON), then sets `payment.state =
    PAYMENT_STATE_PENDING` (matching how pretix's own `stripe` plugin behaves — without this, the
    payment stays in `created`, and pretix's own `OrderPaymentComplete`/`OrderPaymentStart` guards, which
    only reject re-entry once the state has moved *away* from `created`, would let a customer who
    revisits their payment link retrigger `execute_payment` and generate a second reference). Payment
    stays pending until the webhook confirms it.
  - `EupagoMBWAY` — checkout form collects a phone number (also has a `payment_prepare` path for the
    "retry payment" flow on the order page); `execute_payment` normalizes the phone to euPago's
    `351#XXXXXXXXX` format, calls `mbway/create`, and likewise sets `payment.state =
    PAYMENT_STATE_PENDING` for the same re-entry reason as Multibanco — for MB WAY this specifically
    guards against a revisit sending a duplicate push notification to the customer's phone. Also pending
    until webhook confirmation.
  - Both providers implement `payment_pending_render` / `payment_control_render` (per-provider
    templates), `api_payment_details`, `matching_id`, and `shred_payment_info` (MB WAY redacts the
    phone number; Multibanco has nothing to shred).
  - Neither provider implements `execute_refund` — refunds can't be *initiated* from pretix and must be
    handled manually/out-of-band in euPago's Backoffice. Refunds triggered that way *are* reflected back
    into pretix, though: see `EupagoWebhookView._refund_payment` below.
- `EupagoSettingsMixin` (top of `payment.py`) holds only what's genuinely shared: `_api_key`,
  `_is_sandbox`, `_env()`, `test_mode_message`, and `settings_content_render` (a link back to the
  shared settings page, shown on each provider's own settings screen so admins don't go looking for a
  field that isn't there anymore). It deliberately does **not** override `settings_form_fields` — see
  below for why the API key/sandbox fields live elsewhere.
- `pretix_eupago/views.py` also defines `EupagoSettingsForm` (a `pretix.base.forms.SettingsForm`) and
  `EupagoSettings` (`EventSettingsViewMixin` + `EventSettingsFormView`) — a single Control-panel page,
  shared across both payment methods, holding `eupago_api_key`, `eupago_sandbox`, and
  `eupago_webhook_secret` (optional; see webhook section below). These are read directly off
  `event.settings` (not a provider's `SettingsSandbox`) by `EupagoSettingsMixin`, since
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
- `pretix_eupago/views.py` also defines `EupagoOrdersView` (`EventPermissionRequiredMixin` +
  `ListView`, `permission = "event.orders:read"`), a Control-panel page listing every
  `OrderPayment` for the event with `provider` in `eupago_multibanco`/`eupago_mbway`, alongside the
  method-specific data read straight from `payment.info_data` (Multibanco entidade/referencia, MB
  WAY transactionID) — the same data the webhook handler and order detail page use. Filterable by
  provider/state via GET params, paginated.
  - `pretix_eupago/urls.py` registers it at `control/event/<organizer>/<event>/eupago/orders`
    (same non-`event_patterns` pattern as the settings page above).
  - `pretix_eupago/signals.py` adds a `nav_event` receiver (not `settings_links`, since this is a
    full page, not a settings form) to put "euPago orders" in the event's Control-panel sidebar,
    following the same pattern as pretix's own `banktransfer` plugin's `nav_event` receiver.
  - `pretix_eupago/templates/pretix_eupago/orders.html` extends `pretixcontrol/event/base.html`
    (the plain-page shell, as opposed to `settings_base.html` used by the settings page) and reuses
    `pretixcontrol/pagination.html` for paging.
- `pretix_eupago/views.py` — `EupagoWebhookView`, a CSRF-exempt Django view handling **both** euPago
  webhook formats (see https://eupago.readme.io/reference/webhooks and
  https://eupago.readme.io/reference/realtime-webhooks-20):
  - v1.0: `GET` with query-string params (`identificador`, `mp`, `chave_api`, ...). Only sent for paid
    references — euPago does not send v1.0 notifications for expired/canceled/refunded ones.
  - v2.0: `POST` with a JSON body (`transaction.status/identifier/method`), statuses `Paid`, `Refund`,
    `Error`, `Cancel`, `Expired`. Note the real payload key is singular `transaction`, even though
    euPago's own docs at the link above call it `transactions` (plural) — verified against a live
    webhook payload.
  Both paths funnel through `_lookup_payment(identifier)`, which parses the identifier as
  `"{order.code}-{payment.pk}"` (via `rsplit("-", 1)`), looks up the `OrderPayment`, and cross-checks the
  identifier stored in `payment.info_data`. That cross-check alone is **not** sufficient authentication —
  a payment's own customer already knows their own order code and payment pk, so they could replay it
  against the global endpoint to self-confirm an unpaid order. Each webhook format therefore adds a
  further, source-specific check before acting on the result:
  - **v1.0 (`get`)**: euPago includes `chave_api`, the API key used to create the reference, in every
    v1.0 notification. That key is a secret only the event admin and euPago know (customers never see
    it), so `get()` requires it to `hmac.compare_digest`-match the event's configured `eupago_api_key`
    before calling `_confirm_payment`. This check is mandatory — there's no opt-out, since `chave_api` is
    always present in genuine v1.0 traffic.
  - **v2.0 (`post`)**: euPago signs the raw POST body with HMAC-SHA256 in an `X-Signature` header
    (base64-encoded digest), keyed with a secret generated per-channel in Backoffice. If the event has
    that secret configured (`eupago_webhook_secret`, optional), `post()` verifies the signature via
    `_verify_hmac()` before acting and rejects on mismatch. If the secret is *not* configured (e.g. an
    upgrade from before this existed), it logs a warning and falls back to the identifier cross-check
    only — kept as a fallback rather than a hard requirement so existing installs don't lose webhook
    confirmation entirely until an admin sets the secret.
  - `post()` handles `status in ("Cancel", "Canceled", "Expired")` via `_fail_payment`, which calls
    pretix core's `payment.fail()` (state → `PAYMENT_STATE_FAILED`, merging the euPago status into
    `payment.info_data` rather than overwriting it). This is the MB WAY "customer canceled the push in
    the app" path (euPago sends a v2.0 `Cancel`), and also covers an expired Multibanco reference.
    `fail()` only acts on created/pending/canceled payments and is race-safe against a simultaneous
    confirmation, so a stray `Cancel`/`Expired` arriving after `Paid` is a no-op. No refund is
    involved — the payment never completed.
  - `post()` also handles `status in ("Refund", "Refunded")` via `_refund_payment`, which calls
    `payment.create_external_refund()` (pretix core's designated API for refunds triggered by an
    external source — the same one the in-tree `stripe` plugin uses) when the payment is currently
    confirmed and has no refund recorded yet. euPago's payload carries no per-refund id/amount, so this
    always treats it as a full refund and is a no-op if a refund already exists (webhook retries, or a
    second notification, don't double-refund).
  - **v2.0 encrypted mode**: euPago's Backoffice has an optional per-channel "encrypt" toggle for
    webhooks; when enabled, the POST body is `{"data": "<base64 AES-256-CBC ciphertext>", ...}` (no
    plaintext `transaction`) with the IV in an `X-Initialization-Vector` header, keyed with the same
    per-channel secret as `X-Signature`. `post()` detects this (`"data" in data and "transaction" not
    in data`) and calls `_decrypt_payload()`, which — since this is one global endpoint shared by every
    event and there's no way to know which event a request belongs to before decrypting it — tries every
    event's configured `eupago_webhook_secret` in turn via `_aes_cbc_decrypt()` until one produces
    correctly-PKCS7-padded plaintext. A successful decrypt is itself strong proof of authenticity (wrong
    keys reliably fail PKCS7 unpadding), so the normal `X-Signature` check is skipped for this path. This
    requires the `cryptography` package, declared as a direct dependency (not just relied on transitively
    via pretix) since the plugin imports it directly.
  - The whole view is decorated with `@method_decorator(scopes_disabled(), name="dispatch")`
    (`django_scopes`). This is required, not optional: `/eupago/webhook/` is a global URL with no
    `organizer`/`event` in its path, so pretix's usual scope-activation (which scopes queries by the
    matched URL kwargs) never runs for this view. Without `scopes_disabled()`, any `OrderPayment`
    query raises `django_scopes.exceptions.ScopeError`, which the broad `except Exception` in
    `_lookup_payment` silently swallows — the webhook returns `200 OK` but never actually confirms
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

### Multibanco reference delivery by email

`EupagoMultibanco.order_pending_mail_render` is `BasePaymentProvider`'s standard hook for the
`{payment_info}` placeholder in pretix's own "order placed" email — but pretix core
(`_perform_order` in `pretix.base.services.orders`) always sends that email *before* calling
`execute_payment` for any provider that doesn't set `execute_payment_needs_user = False` (the
default, which this plugin doesn't override, since `execute_payment` here does need the real
`request`/session for MB WAY's phone fallback and there's no supported pretix hook to run a
third-party provider's `execute_payment` earlier — that early-execution path in `_perform_order`
is hard-coded to the built-in `GiftCardPayment` only). So by the time that first email is
rendered, `payment.info` is still empty and `order_pending_mail_render` correctly returns `""` —
the reference isn't lost, it just doesn't exist yet. Relying on `order_pending_mail_render` alone
would mean the customer never receives it by email at all.

To work around this, `EupagoMultibanco.execute_payment` sends its own follow-up email directly via
`payment.order.send_mail(..., template="pretix_eupago/mail_multibanco.txt", context={"info": info,
"payment": payment})` right after the reference is generated. `order_pending_mail_render` is kept
too — it's still what populates `{payment_info}` on other emails sent *after* `execute_payment` has
already run (e.g. an admin manually resending the order confirmation from Control). MB WAY doesn't
need an equivalent: it's a push payment with nothing to display, so there's no email content it
would add either way.

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

## Translations

`pretix_eupago/locale/<lang>/LC_MESSAGES/` holds gettext catalogs (currently `pt_PT`). Since
`pretix_eupago` is a real Django app (see "Plugin registration mechanics" above), Django's i18n
machinery discovers this automatically — no pretix-specific wiring needed, `{% load i18n %}` /
`_()` / `gettext_lazy()` calls throughout the codebase just work once a `.mo` file exists.

- `make translate` — extracts `_()`/`gettext_lazy()`/`{% trans %}` strings into
  `locale/<lang>/LC_MESSAGES/django.po` (merging into any existing translations).
- `make compile-translations` — compiles `.po` → `.mo`. **Must run before `uv build`** — only
  `.mo` files are loaded at runtime, and `package-data` in `pyproject.toml` just ships whatever's
  already on disk; it doesn't compile anything.
- To add a language: `mkdir -p pretix_eupago/locale/<lang>/LC_MESSAGES`, add it to `LOCALES` in
  the `Makefile`, then `make translate`.
- Both targets deliberately configure a bare `django.conf.settings.configure(USE_I18N=True)`
  instead of pointing `DJANGO_SETTINGS_MODULE` at `tests.settings` (i.e. pretix's own settings).
  pretix's `settings.py` sets `LOCALE_PATHS` to pretix core's *own* bundled locale directory, and
  `makemessages`/`compilemessages` treat every `LOCALE_PATHS` entry as a target, not just the
  current app's — running them against pretix's settings module extracts this plugin's strings
  *into pretix core's own installed catalogs* (`.venv/…/pretix/locale/*/LC_MESSAGES/django.po`),
  corrupting the installed pretix package. Discovered the hard way; if `.venv`'s pretix install
  ever turns up with `euPago` strings in unrelated languages, `rm -rf .venv && uv sync --extra
  test` restores a clean copy.

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

- `lint` / `test` — run on every push to `main`, every pull request, and every GitHub Release. `lint`
  runs `ruff check` and `ruff format --check`; `test` runs `uv sync --extra test` then `uv run pytest`,
  matrixed across Python 3.11–3.14 (matching `requires-python` and the Python version classifiers in
  `pyproject.toml`).
- `build` / `publish` — only run for the `release` event (`if: github.event_name == 'release'`), and
  `needs: [lint, test]`, so a release only reaches PyPI if both lint and the test suite pass.
  `build` runs `uv build`. Publishing uses PyPI **trusted publishing** (OIDC) — no API token is
  stored as a repo secret. The `publish` job's `environment: pypi` must match the environment name
  registered as the trusted publisher on PyPI's project settings
  (https://pypi.org/manage/account/publishing/).

Bump the version in both `pyproject.toml` and `pretix_eupago/__init__.py` before cutting a release —
run `make bump-version <version>` (e.g. `make bump-version 1.2.0`) rather than editing them by hand, so
the two stay in sync. It doesn't touch `uv.lock`; run `uv lock` afterward so the lockfile's own
`pretix-eupago` entry matches.
