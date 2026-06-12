"""Tests for the bundled dashboard frontend registration (modules + Lovelace resources)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.energy_conductor import (
    _LONGTERM_URL,
    _MODULE_URLS,
    _STRATEGY_URL,
    _STRATEGY_VERSION,
    _TAPE_URL,
    _async_register_lovelace_resources,
    async_setup,
)
from homeassistant.core import CoreState


async def test_async_setup_noop_without_http() -> None:
    # The test harness / headless setups have no web server — registration is
    # skipped but setup must still succeed.
    hass = MagicMock()
    hass.http = None
    assert await async_setup(hass, {}) is True


async def test_async_setup_registers_static_paths_and_js() -> None:
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        assert await async_setup(hass, {}) is True

    hass.http.async_register_static_paths.assert_awaited_once()
    paths = hass.http.async_register_static_paths.call_args[0][0]
    assert [p.url_path for p in paths] == [_STRATEGY_URL, _LONGTERM_URL, _TAPE_URL]
    added = [call.args[1] for call in add_js.call_args_list]
    assert added == [f"{url}?v={_STRATEGY_VERSION}" for url in _MODULE_URLS]


async def test_async_setup_survives_registration_failure() -> None:
    # A frontend registration failure must never take the integration down.
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("boom"))
    assert await async_setup(hass, {}) is True


class _FakeResources:
    """Storage-mode Lovelace resource collection double."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self.loaded = True
        self._items = items or []
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def async_items(self) -> list[dict]:
        return self._items

    async def async_create_item(self, target: dict) -> None:
        self.created.append(target)

    async def async_update_item(self, item_id: str, target: dict) -> None:
        self.updated.append((item_id, target))


def _hass_with_resources(resources) -> MagicMock:
    hass = MagicMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data = {"lovelace": lovelace}
    return hass


async def test_lovelace_resources_created_on_first_start() -> None:
    resources = _FakeResources()
    await _async_register_lovelace_resources(_hass_with_resources(resources))

    assert [r["url"] for r in resources.created] == [
        f"{url}?v={_STRATEGY_VERSION}" for url in _MODULE_URLS
    ]
    assert all(r["res_type"] == "module" for r in resources.created)
    assert resources.updated == []


async def test_lovelace_resources_version_bumped_not_duplicated() -> None:
    resources = _FakeResources(
        items=[
            {"id": "abc", "url": f"{_STRATEGY_URL}?v=0", "res_type": "module"},
            {"id": "def", "url": f"{_LONGTERM_URL}?v={_STRATEGY_VERSION}", "res_type": "module"},
            {"id": "ghi", "url": f"{_TAPE_URL}?v={_STRATEGY_VERSION}", "res_type": "module"},
            {"id": "zzz", "url": "/hacsfiles/some-card.js", "res_type": "module"},
        ]
    )
    await _async_register_lovelace_resources(_hass_with_resources(resources))

    # Stale strategy entry updated in place; current long-term entry untouched;
    # foreign resources never touched; nothing re-created.
    assert resources.created == []
    assert resources.updated == [
        ("abc", {"res_type": "module", "url": f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}"})
    ]


async def test_lovelace_resources_yaml_mode_left_alone() -> None:
    # YAML-mode resource lists are user-managed: no async_create_item, no action.
    hass = MagicMock()
    lovelace = MagicMock(spec=[])  # no .resources attribute at all
    hass.data = {"lovelace": lovelace}
    await _async_register_lovelace_resources(hass)  # must simply not raise


class _LovelaceItem:
    """Minimal stand-in for the HA storage-mode Lovelace resource dataclass."""

    def __init__(self, id: str, url: str) -> None:
        self.id = id
        self.url = url


async def test_lovelace_resources_dataclass_items_handled() -> None:
    """HA production returns dataclass instances with .url/.id — not dicts.

    Calling .get() on a dataclass raises AttributeError; the registration
    loop must use attribute access for non-dict items.
    """
    items: list = [
        _LovelaceItem("abc", f"{_STRATEGY_URL}?v=0"),
        _LovelaceItem("def", f"{_LONGTERM_URL}?v={_STRATEGY_VERSION}"),
        _LovelaceItem("ghi", f"{_TAPE_URL}?v={_STRATEGY_VERSION}"),
    ]
    resources = _FakeResources(items=items)
    await _async_register_lovelace_resources(_hass_with_resources(resources))

    assert resources.created == []
    assert resources.updated == [
        ("abc", {"res_type": "module", "url": f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}"})
    ]


async def test_lovelace_resources_failure_tolerated() -> None:
    resources = _FakeResources()
    resources.async_items = MagicMock(side_effect=RuntimeError("boom"))
    await _async_register_lovelace_resources(_hass_with_resources(resources))


async def test_setup_defers_resource_registration_until_started() -> None:
    """Lovelace sets up its store during bootstrap; registration waits for the
    HA started event (or runs immediately on a reload of a running instance)."""
    hass = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.state = CoreState.not_running
    with patch("homeassistant.components.frontend.add_extra_js_url"):
        assert await async_setup(hass, {}) is True
    hass.bus.async_listen_once.assert_called_once()

    hass2 = MagicMock()
    hass2.http.async_register_static_paths = AsyncMock()
    hass2.state = CoreState.running
    resources = _FakeResources()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass2.data = {"lovelace": lovelace}
    with patch("homeassistant.components.frontend.add_extra_js_url"):
        assert await async_setup(hass2, {}) is True
    assert len(resources.created) == len(_MODULE_URLS)
