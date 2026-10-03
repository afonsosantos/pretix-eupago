import pytest
from django_scopes import scopes_disabled
from pretix.base.models import Team, User

from pretix_eupago.payment import EupagoMBWAY, EupagoMultibanco

SETTINGS_URL = "/control/event/{}/{}/eupago/settings"


@pytest.fixture
def logged_in_client(client, event):
    with scopes_disabled():
        user = User.objects.create_user("dummy@example.org", "dummy")
        team = Team.objects.create(
            organizer=event.organizer, all_event_permissions=True
        )
        team.members.add(user)
        team.limit_events.add(event)
    client.login(email="dummy@example.org", password="dummy")
    return client


@pytest.mark.django_db
def test_get_settings_page(logged_in_client, event):
    response = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "API key" in content
    assert "Webhook key" in content
    # The URL to paste into euPago's Backoffice is shown, not buried in a code comment.
    assert "/eupago/webhook/" in content
    # No webhook key yet: warned about, since Paid notifications are refused without it.
    assert "No webhook key is set" in content


@pytest.mark.django_db
def test_settings_page_is_in_settings_sidebar(logged_in_client, event):
    response = logged_in_client.get(
        f"/control/event/{event.organizer.slug}/{event.slug}/settings/"
    )
    assert response.status_code == 200
    assert SETTINGS_URL.format(event.organizer.slug, event.slug) in (
        response.content.decode()
    )


@pytest.mark.django_db
def test_settings_page_masks_secrets(logged_in_client, event):
    with scopes_disabled():
        event.settings.set("eupago_api_key", "super-secret-api-key")
        event.settings.set("eupago_webhook_secret", "super-secret-webhook-key")

    response = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    )

    assert b"super-secret-api-key" not in response.content
    assert b"super-secret-webhook-key" not in response.content
    assert b"No webhook key is set" not in response.content


@pytest.mark.django_db
def test_settings_page_404s_when_plugin_disabled(logged_in_client, event):
    with scopes_disabled():
        event.plugins = ""
        event.save()

    response = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_post_settings_page_saves_and_is_shared_by_both_providers(
    logged_in_client, event
):
    response = logged_in_client.post(
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        data={"eupago_api_key": "posted-api-key", "eupago_sandbox": "on"},
    )
    assert response.status_code == 302

    with scopes_disabled():
        event.settings.flush()

    multibanco = EupagoMultibanco(event)
    mbway = EupagoMBWAY(event)
    assert multibanco._api_key == "posted-api-key"
    assert mbway._api_key == "posted-api-key"
    assert multibanco._is_sandbox is True
    assert mbway._is_sandbox is True


@pytest.mark.django_db
def test_post_settings_page_saves_webhook_secret(logged_in_client, event):
    response = logged_in_client.post(
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        data={
            "eupago_api_key": "posted-api-key",
            "eupago_webhook_secret": "posted-webhook-secret",
        },
    )
    assert response.status_code == 302

    with scopes_disabled():
        event.settings.flush()
        assert event.settings.get("eupago_webhook_secret") == "posted-webhook-secret"


@pytest.mark.django_db
def test_settings_page_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.get(SETTINGS_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 404


@pytest.mark.django_db
def test_api_key_and_sandbox_are_not_duplicated_per_provider(event):
    multibanco = EupagoMultibanco(event)
    mbway = EupagoMBWAY(event)

    with scopes_disabled():
        assert "api_key" not in multibanco.settings_form_fields
        assert "sandbox" not in multibanco.settings_form_fields
        assert "api_key" not in mbway.settings_form_fields
        assert "sandbox" not in mbway.settings_form_fields
        # Multibanco keeps its own provider-specific field.
        assert "multibanco_expiry_days" in multibanco.settings_form_fields


@pytest.mark.django_db
def test_settings_content_render_links_to_shared_settings_page(event):
    provider = EupagoMultibanco(event)
    html = provider.settings_content_render(None)
    assert SETTINGS_URL.format(event.organizer.slug, event.slug) in html
