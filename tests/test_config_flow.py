"""Tests for the Trimlight Edge config flow."""
import logging
import pathlib
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trimlight.api import TrimlightApiError, TrimlightAuthError
from custom_components.trimlight.const import (
    API_CREDENTIALS_URL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DOMAIN,
)

from .conftest import MOCK_DEVICE_LIST

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

VALID_INPUT = {CONF_CLIENT_ID: "test_id", CONF_CLIENT_SECRET: "test_secret"}


async def test_user_step_shows_form(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_links_to_the_credentials_form(hass):
    """The setup form links to Trimlight's credentials application form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["description_placeholders"] == {"apply_url": API_CREDENTIALS_URL}


def test_translated_strings_contain_no_urls():
    """Hassfest rejects URLs in translated strings; they go in placeholders.

    Regression test for the Validate workflow failing with "Invalid
    strings.json: the string should not contain URLs, please use description
    placeholders instead" after the credentials link was added inline.
    """
    root = pathlib.Path(__file__).parent.parent / "custom_components" / "trimlight"
    for name in ("strings.json", "translations/en.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, (
            f"{name} contains a URL; pass it as a description placeholder"
        )


async def test_user_step_success(hass):
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )
        await hass.async_block_till_done()

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Trimlight (test_id)"
        assert result["data"] == VALID_INPUT

        # Remove entry while patches are active to prevent teardown errors.
        await hass.config_entries.async_remove(result["result"].entry_id)
        await hass.async_block_till_done()


async def test_user_step_cannot_connect(hass):
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=TrimlightApiError("fail"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_invalid_auth(hass):
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(
            side_effect=TrimlightAuthError("API auth error 10001: auth error")
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_step_unknown_error(hass):
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=Exception("boom"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_already_configured_aborts(hass):
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        # First setup
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        first_result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )
        await hass.async_block_till_done()

        # Second setup with same client_id
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        # The duplicate is caught before another round trip to the cloud.
        assert mock_api.get_devices.await_count == 1

        # Cleanup entry while patches are active.
        await hass.config_entries.async_remove(first_result["result"].entry_id)
        await hass.async_block_till_done()


def _suggested(result) -> dict:
    """Return the suggested values carried by a shown form's schema."""
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if getattr(key, "description", None)
        and "suggested_value" in key.description
    }


async def test_retry_after_a_failed_attempt_is_not_blocked(hass):
    """A second attempt with the same Client ID must not dead-end.

    Regression test: the duplicate check runs before validation, so a failed
    attempt leaves its flow in progress holding the unique id. With the
    default raise_on_progress the user's next attempt with the same Client ID
    aborted on "already_in_progress" — a reason with no entry in strings.json,
    so the dialog showed the bare key and there was no way forward.
    """
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(
            side_effect=TrimlightAuthError("API auth error 10001: auth error")
        )

        # First attempt: wrong secret. The form comes back with an error and
        # this flow stays in progress.
        first = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"], user_input=VALID_INPUT
        )
        assert first["type"] == FlowResultType.FORM
        assert first["errors"] == {"base": "invalid_auth"}

        # The user starts over rather than reusing that dialog.
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)
        second = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"], user_input=VALID_INPUT
        )
        await hass.async_block_till_done()

        assert second["type"] == FlowResultType.CREATE_ENTRY
        assert second["data"] == VALID_INPUT

        await hass.config_entries.async_remove(second["result"].entry_id)
        await hass.async_block_till_done()


async def test_form_keeps_credentials_after_an_error(hass):
    """A re-shown form carries the pasted values back, trimmed."""
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=TrimlightApiError("fail"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        # The first form is empty; nothing has been typed yet.
        assert _suggested(result) == {}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "  test_id\n",
                CONF_CLIENT_SECRET: "test_secret ",
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert _suggested(result) == VALID_INPUT


async def test_user_step_strips_pasted_whitespace(hass):
    """Credentials pasted with surrounding whitespace or a newline still work."""
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_CLIENT_ID: "  test_id\n",
                CONF_CLIENT_SECRET: "test_secret \n",
            },
        )
        await hass.async_block_till_done()

        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Trimlight (test_id)"
        assert result["data"] == VALID_INPUT
        assert result["result"].unique_id == "test_id"
        mock_api_cls.assert_called_once_with("test_id", "test_secret", ANY)

        await hass.config_entries.async_remove(result["result"].entry_id)
        await hass.async_block_till_done()


async def test_reauth_flow_strips_pasted_whitespace(hass):
    """A re-entered secret is trimmed the same way as on first setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Trimlight (test_id)",
        data=dict(VALID_INPUT),
        unique_id="test_id",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_SECRET: " new_secret\n"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_SECRET] == "new_secret"
    mock_api_cls.assert_called_once_with("test_id", "new_secret", ANY)


async def test_unknown_validation_error_is_logged(hass, caplog):
    """An unexpected exception during validation is logged, not swallowed."""
    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(side_effect=RuntimeError("boom"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=VALID_INPUT
        )

    assert result["errors"] == {"base": "unknown"}
    assert any(
        r.levelno == logging.ERROR and r.exc_info and "boom" in str(r.exc_info[1])
        for r in caplog.records
    )


async def test_reauth_flow_updates_secret(hass):
    """A reauth prompted by rejected credentials stores the new secret."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Trimlight (test_id)",
        data=dict(VALID_INPUT),
        unique_id="test_id",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.trimlight.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.trimlight.async_unload_entry",
            return_value=True,
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(return_value=MOCK_DEVICE_LIST)

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_SECRET: "new_secret"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_SECRET] == "new_secret"
    # The client id can't be changed via reauth.
    assert entry.data[CONF_CLIENT_ID] == "test_id"


async def test_reauth_flow_rejects_bad_secret(hass):
    """A reauth attempt with still-bad credentials re-shows the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Trimlight (test_id)",
        data=dict(VALID_INPUT),
        unique_id="test_id",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.trimlight.config_flow.TrimlightApi"
        ) as mock_api_cls,
        patch(
            "custom_components.trimlight.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        mock_api = mock_api_cls.return_value
        mock_api.get_devices = AsyncMock(
            side_effect=TrimlightAuthError("API auth error 10001: auth error")
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_CLIENT_SECRET: "still_wrong"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_CLIENT_SECRET] == "test_secret"
