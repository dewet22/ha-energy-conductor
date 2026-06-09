"""Tests for the bundled dashboard-strategy frontend registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.energy_conductor import (
    _STRATEGY_URL,
    _STRATEGY_VERSION,
    async_setup,
)


async def test_async_setup_noop_without_http() -> None:
    # The test harness / headless setups have no web server — registration is
    # skipped but setup must still succeed.
    hass = MagicMock()
    hass.http = None
    assert await async_setup(hass, {}) is True


async def test_async_setup_registers_static_path_and_js() -> None:
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        assert await async_setup(hass, {}) is True

    hass.http.async_register_static_paths.assert_awaited_once()
    paths = hass.http.async_register_static_paths.call_args[0][0]
    assert paths[0].url_path == _STRATEGY_URL
    add_js.assert_called_once_with(hass, f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}")


async def test_async_setup_survives_registration_failure() -> None:
    # A frontend registration failure must never take the integration down.
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("boom"))
    assert await async_setup(hass, {}) is True
