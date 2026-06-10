"""Config flow for the energy_conductor integration.

Both the initial wizard and the options flow are built from ONE set of per-group schema
builders (battery / tariff / solar / loads / ev / behaviour), so every field is editable in
both places (full parity) without duplicating field definitions. The wizard walks the groups
as required steps; the options flow presents them as a menu of focused sub-steps.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TimeSelector,
)

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_POWER_POSITIVE_IS_CHARGING,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_DEVICE_NAME,
    CONF_DISPATCHING_SENSOR,
    CONF_ENTITY_REFS,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HOME_LOAD_SENSOR,
    CONF_HOTWATER_CAPACITY_KWH,
    CONF_HOTWATER_DEPLETION_KWH,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_HOTWATER_HEATER_KW,
    CONF_HOTWATER_MAX_TEMP_STATE,
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_HOTWATER_THRESHOLD_PERCENT,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_RESERVE_SOC_SENSOR,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_SOUTHERN_HEMISPHERE,
    CONF_SUMMER_MAX_KWH,
    CONF_WINTER_MIN_KWH,
    CONF_WRITE_MODE,
    DEFAULT_DAILY_KWH_TARGET,
    DEFAULT_EV_MIN_ACTIVATION_W,
    DEFAULT_HOTWATER_CAPACITY_KWH,
    DEFAULT_HOTWATER_DEPLETION_KWH,
    DEFAULT_HOTWATER_HEATER_KW,
    DEFAULT_HOTWATER_MAX_TEMP_STATE,
    DEFAULT_HOTWATER_THRESHOLD_PERCENT,
    DEFAULT_OVERNIGHT_PLAN_TIME,
    DEFAULT_OVERNIGHT_WINDOW_END_TIME,
    DEFAULT_RESERVE_PERCENT,
    DEFAULT_SUMMER_MAX_KWH,
    DEFAULT_WINTER_MIN_KWH,
    DOMAIN,
    FORECAST_SOURCE_DAILY,
    FORECAST_SOURCE_NONE,
    FORECAST_SOURCE_SOLCAST,
    WRITE_MODE_DRY_RUN,
    WRITE_MODE_LIVE,
)
from .entity_ref import (
    LIST_ENTITY_CONF_KEYS,
    SCALAR_ENTITY_CONF_KEYS,
    capture_all,
    capture_ref,
)

# Keys persisted by each options sub-step (the single source of truth for group membership;
# the schema builders below render exactly these). Solar is split across two sub-steps.
BATTERY_KEYS = (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_RESERVE_SOC_SENSOR,
)
TARIFF_KEYS = (CONF_OFF_PEAK_SENSOR, CONF_DISPATCHING_SENSOR, CONF_OVERNIGHT_WINDOW_END_TIME)
SOLAR_KEYS = (
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_WINTER_MIN_KWH,
    CONF_SUMMER_MAX_KWH,
    CONF_SOUTHERN_HEMISPHERE,
)
LOADS_KEYS = (
    CONF_HOME_LOAD_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DAILY_KWH_TARGET,
)
EV_KEYS = (CONF_EV_POWER_SENSOR, CONF_EV_MIN_ACTIVATION_W)
GRID_KEYS = (
    CONF_GRID_IMPORT_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_POWER_POSITIVE_IS_CHARGING,
)
HOTWATER_KEYS = (
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_CAPACITY_KWH,
    CONF_HOTWATER_HEATER_KW,
    CONF_HOTWATER_THRESHOLD_PERCENT,
    CONF_HOTWATER_DEPLETION_KWH,
    CONF_HOTWATER_MAX_TEMP_STATE,
)
BEHAVIOUR_KEYS = (
    CONF_WRITE_MODE,
    CONF_NOTIFY_TARGET,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_DEVICE_NAME,
)

_NO_DEFAULT = object()


# ---- selector helpers -------------------------------------------------------------------


def _sensor_selector(device_class: str | None = None) -> EntitySelector:
    cfg: dict[str, Any] = {"domain": "sensor"}
    if device_class:
        cfg["device_class"] = device_class
    return EntitySelector(EntitySelectorConfig(**cfg))


def _number_entity_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="number"))


def _soc_floor_selector() -> EntitySelector:
    # The minimum-SoC floor may be exposed as a `number` (GivEnergy battery_soc_reserve)
    # or a `sensor`, depending on the integration. Accept either.
    return EntitySelector(EntitySelectorConfig(domain=["sensor", "number"]))


def _binary_sensor_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="binary_sensor"))


def _managed_loads_selector() -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
    )


def _percent_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=100, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
        )
    )


def _kwh_selector(*, min_value: float = 0, max_value: float = 100) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=0.1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="kWh",
        )
    )


def _kw_selector(*, min_value: float = 0.5, max_value: float = 10) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=0.1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="kW",
        )
    )


def _watts_selector(*, min_value: int = 0, max_value: int = 10000) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=10,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="W",
        )
    )


def _write_mode_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[WRITE_MODE_DRY_RUN, WRITE_MODE_LIVE],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="write_mode",
        )
    )


def _forecast_source_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[FORECAST_SOURCE_SOLCAST, FORECAST_SOURCE_DAILY, FORECAST_SOURCE_NONE],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="forecast_source",
        )
    )


# ---- shared schema builders -------------------------------------------------------------


def _marker(
    key: str,
    *,
    options: bool,
    defaults: dict[str, Any],
    required: bool = False,
    default: Any = _NO_DEFAULT,
) -> vol.Marker:
    """Build a voluptuous key marker shared by the wizard and options flows.

    Wizard mode (`options=False`): `vol.Required`/`vol.Optional` with the field's static
    default. Options mode (`options=True`): always `vol.Optional` pre-filled with the stored
    value via `suggested_value`, so leaving a field untouched preserves it.
    """
    if options:
        return vol.Optional(key, description={"suggested_value": defaults.get(key)})
    marker = vol.Required if required else vol.Optional
    if default is _NO_DEFAULT:
        return marker(key)
    return marker(key, default=default)


def battery_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(CONF_BATTERY_SOC_SENSOR, options=options, defaults=defaults, required=True): (
                _sensor_selector(device_class="battery")
            ),
            _marker(
                CONF_BATTERY_CHARGE_CONTROL, options=options, defaults=defaults, required=True
            ): _number_entity_selector(),
            _marker(
                CONF_BATTERY_DISCHARGE_LIMIT, options=options, defaults=defaults, required=True
            ): _number_entity_selector(),
            _marker(
                CONF_BATTERY_CAPACITY_KWH, options=options, defaults=defaults, required=True
            ): _kwh_selector(min_value=0.5, max_value=100),
            _marker(
                CONF_BATTERY_RESERVE_PERCENT,
                options=options,
                defaults=defaults,
                required=True,
                default=DEFAULT_RESERVE_PERCENT,
            ): _percent_selector(),
            _marker(CONF_RESERVE_SOC_SENSOR, options=options, defaults=defaults): (
                _soc_floor_selector()
            ),
        }
    )


def grid_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    """Optional meter-side input for read-only observability + actuation verification."""
    return vol.Schema(
        {
            _marker(CONF_GRID_IMPORT_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="power")
            ),
            _marker(CONF_GRID_EXPORT_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="power")
            ),
            _marker(CONF_BATTERY_POWER_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="power")
            ),
            _marker(
                CONF_BATTERY_POWER_POSITIVE_IS_CHARGING,
                options=options,
                defaults=defaults,
                default=False,
            ): BooleanSelector(),
        }
    )


def tariff_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(CONF_OFF_PEAK_SENSOR, options=options, defaults=defaults, required=True): (
                _binary_sensor_selector()
            ),
            _marker(CONF_DISPATCHING_SENSOR, options=options, defaults=defaults): (
                _binary_sensor_selector()
            ),
            _marker(
                CONF_OVERNIGHT_WINDOW_END_TIME,
                options=options,
                defaults=defaults,
                required=True,
                default=DEFAULT_OVERNIGHT_WINDOW_END_TIME.isoformat(),
            ): TimeSelector(),
        }
    )


def forecast_source_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(
                CONF_FORECAST_SOURCE,
                options=options,
                defaults=defaults,
                required=True,
                default=FORECAST_SOURCE_SOLCAST,
            ): _forecast_source_selector(),
        }
    )


def forecast_schema(source: str, defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    fields: dict[Any, Any] = {
        _marker(CONF_SOLAR_GENERATION_SENSOR, options=options, defaults=defaults): (
            _sensor_selector()
        ),
        _marker(
            CONF_WINTER_MIN_KWH,
            options=options,
            defaults=defaults,
            required=True,
            default=DEFAULT_WINTER_MIN_KWH,
        ): _kwh_selector(max_value=50),
        _marker(
            CONF_SUMMER_MAX_KWH,
            options=options,
            defaults=defaults,
            required=True,
            default=DEFAULT_SUMMER_MAX_KWH,
        ): _kwh_selector(max_value=100),
        _marker(
            CONF_SOUTHERN_HEMISPHERE,
            options=options,
            defaults=defaults,
            required=True,
            default=False,
        ): BooleanSelector(),
    }
    if source == FORECAST_SOURCE_SOLCAST:
        fields[
            _marker(CONF_FORECAST_SOLCAST_SENSOR, options=options, defaults=defaults, required=True)
        ] = _sensor_selector()
    elif source == FORECAST_SOURCE_DAILY:
        fields[
            _marker(CONF_FORECAST_DAILY_SENSOR, options=options, defaults=defaults, required=True)
        ] = _sensor_selector()
    return vol.Schema(fields)


def loads_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(CONF_HOME_LOAD_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="power")
            ),
            _marker(CONF_MANAGED_LOAD_SENSORS, options=options, defaults=defaults): (
                _managed_loads_selector()
            ),
            _marker(CONF_DAILY_ENERGY_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="energy")
            ),
            _marker(
                CONF_DAILY_KWH_TARGET,
                options=options,
                defaults=defaults,
                required=True,
                default=DEFAULT_DAILY_KWH_TARGET,
            ): _kwh_selector(max_value=200),
        }
    )


def ev_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(CONF_EV_POWER_SENSOR, options=options, defaults=defaults): _sensor_selector(),
            _marker(
                CONF_EV_MIN_ACTIVATION_W,
                options=options,
                defaults=defaults,
                default=DEFAULT_EV_MIN_ACTIVATION_W,
            ): _watts_selector(min_value=100, max_value=11000),
        }
    )


def hotwater_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    """Hot-water diverter (Eddi). All optional — feature inert unless green + status are set."""
    return vol.Schema(
        {
            _marker(CONF_HOTWATER_GREEN_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="energy")
            ),
            _marker(CONF_HOTWATER_STATUS_SENSOR, options=options, defaults=defaults): (
                _sensor_selector()
            ),
            _marker(CONF_HOTWATER_ENERGY_SENSOR, options=options, defaults=defaults): (
                _sensor_selector(device_class="energy")
            ),
            _marker(
                CONF_HOTWATER_CAPACITY_KWH,
                options=options,
                defaults=defaults,
                default=DEFAULT_HOTWATER_CAPACITY_KWH,
            ): _kwh_selector(min_value=1, max_value=50),
            _marker(
                CONF_HOTWATER_HEATER_KW,
                options=options,
                defaults=defaults,
                default=DEFAULT_HOTWATER_HEATER_KW,
            ): _kw_selector(),
            _marker(
                CONF_HOTWATER_THRESHOLD_PERCENT,
                options=options,
                defaults=defaults,
                default=DEFAULT_HOTWATER_THRESHOLD_PERCENT,
            ): _percent_selector(),
            _marker(
                CONF_HOTWATER_DEPLETION_KWH,
                options=options,
                defaults=defaults,
                default=DEFAULT_HOTWATER_DEPLETION_KWH,
            ): _kwh_selector(min_value=0, max_value=30),
            _marker(
                CONF_HOTWATER_MAX_TEMP_STATE,
                options=options,
                defaults=defaults,
                default=DEFAULT_HOTWATER_MAX_TEMP_STATE,
            ): TextSelector(TextSelectorConfig()),
        }
    )


def behaviour_schema(defaults: dict[str, Any], *, options: bool) -> vol.Schema:
    return vol.Schema(
        {
            _marker(
                CONF_WRITE_MODE,
                options=options,
                defaults=defaults,
                required=True,
                default=WRITE_MODE_DRY_RUN,
            ): _write_mode_selector(),
            _marker(CONF_NOTIFY_TARGET, options=options, defaults=defaults, required=True): (
                EntitySelector(EntitySelectorConfig(domain="notify"))
            ),
            _marker(
                CONF_OVERNIGHT_PLAN_TIME,
                options=options,
                defaults=defaults,
                required=True,
                default=DEFAULT_OVERNIGHT_PLAN_TIME.isoformat(),
            ): TimeSelector(),
            _marker(CONF_DEVICE_NAME, options=options, defaults=defaults): (
                TextSelector(TextSelectorConfig())
            ),
        }
    )


def _reanchor(hass, refs: dict[str, Any], key: str, value: Any) -> None:
    """Update the entity_refs map in place for a changed entity reference."""
    if key in SCALAR_ENTITY_CONF_KEYS:
        anchor = capture_ref(hass, value) if value else None
        if anchor:
            refs[key] = anchor
        else:
            refs.pop(key, None)
    elif key in LIST_ENTITY_CONF_KEYS:
        per = {eid: a for eid in (value or []) if (a := capture_ref(hass, eid))}
        if per:
            refs[key] = per
        else:
            refs.pop(key, None)


# ---- flows ------------------------------------------------------------------------------


class EnergyConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_battery(user_input)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="battery", data_schema=battery_schema({}, options=False)
            )
        self._data.update(user_input)
        return await self.async_step_tariff()

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="tariff", data_schema=tariff_schema({}, options=False)
            )
        self._data.update(user_input)
        return await self.async_step_forecast_source()

    async def async_step_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="forecast_source", data_schema=forecast_source_schema({}, options=False)
            )
        self._data.update(user_input)
        return await self.async_step_forecast()

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        source = self._data[CONF_FORECAST_SOURCE]
        if user_input is None:
            return self.async_show_form(
                step_id="forecast", data_schema=forecast_schema(source, {}, options=False)
            )
        self._data.update(user_input)
        return await self.async_step_loads()

    async def async_step_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="loads", data_schema=loads_schema({}, options=False)
            )
        self._data.update(user_input)
        return await self.async_step_ev()

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="ev", data_schema=ev_schema({}, options=False))
        # Only store EV fields when a power sensor is actually configured.
        if user_input.get(CONF_EV_POWER_SENSOR):
            self._data.update({k: v for k, v in user_input.items() if v is not None})
        return await self.async_step_hotwater()

    async def async_step_hotwater(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="hotwater", data_schema=hotwater_schema({}, options=False)
            )
        # Only store hot-water fields when both core sensors (green + status) are set.
        if user_input.get(CONF_HOTWATER_GREEN_SENSOR) and user_input.get(
            CONF_HOTWATER_STATUS_SENSOR
        ):
            self._data.update({k: v for k, v in user_input.items() if v is not None})
        return await self.async_step_grid()

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="grid", data_schema=grid_schema({}, options=False))
        # Store the group only when at least one sensor is set. Grid import/export drive
        # observability (both needed); battery power drives verification (independent) — so
        # don't gate everything on the grid pair, and don't persist a lone default toggle.
        sensors = (CONF_GRID_IMPORT_SENSOR, CONF_GRID_EXPORT_SENSOR, CONF_BATTERY_POWER_SENSOR)
        if any(user_input.get(k) for k in sensors):
            self._data.update({k: v for k, v in user_input.items() if v is not None})
        return await self.async_step_behaviour()

    async def async_step_behaviour(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="behaviour", data_schema=behaviour_schema({}, options=False)
            )
        self._data.update(user_input)
        # Anchor every referenced entity by unique_id so later renames resolve automatically.
        self._data[CONF_ENTITY_REFS] = capture_all(self.hass, self._data)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Energy Conductor", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyConductorOptionsFlow()


class EnergyConductorOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        # In HA 2026.5+, OptionsFlow.config_entry is a read-only property set by the framework
        # before __init__ runs. Access it via self.config_entry in step handlers, not here.
        self._solar_source: str | None = None

    def _defaults(self) -> dict[str, Any]:
        # Merge here (not in __init__) — config_entry is injected after __init__ in 2026.5+.
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, user_input: dict[str, Any], group_keys: tuple[str, ...]) -> ConfigFlowResult:
        """Persist a single group's keys into options, preserving all other groups.

        The full entity_refs map is rebuilt from data+options and stored in options (a whole
        dict, since options replaces data for that key at merge time) with the changed
        group's references re-anchored.
        """
        new_options = {**self.config_entry.options}
        refs: dict[str, Any] = {
            **self.config_entry.data.get(CONF_ENTITY_REFS, {}),
            **self.config_entry.options.get(CONF_ENTITY_REFS, {}),
        }
        for key in group_keys:
            if key in user_input:
                new_options[key] = user_input[key]
                if key in SCALAR_ENTITY_CONF_KEYS or key in LIST_ENTITY_CONF_KEYS:
                    _reanchor(self.hass, refs, key, user_input[key])
        new_options[CONF_ENTITY_REFS] = refs
        return self.async_create_entry(title="", data=new_options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "battery",
                "tariff",
                "solar",
                "loads",
                "ev",
                "hotwater",
                "grid",
                "behaviour",
            ],
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="battery", data_schema=battery_schema(self._defaults(), options=True)
            )
        return self._save(user_input, BATTERY_KEYS)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="tariff", data_schema=tariff_schema(self._defaults(), options=True)
            )
        return self._save(user_input, TARIFF_KEYS)

    async def async_step_solar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="solar", data_schema=forecast_source_schema(self._defaults(), options=True)
            )
        self._solar_source = user_input.get(
            CONF_FORECAST_SOURCE, self._defaults().get(CONF_FORECAST_SOURCE)
        )
        return await self.async_step_solar_details()

    async def async_step_solar_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        source = self._solar_source or FORECAST_SOURCE_NONE
        if user_input is None:
            return self.async_show_form(
                step_id="solar_details",
                data_schema=forecast_schema(source, self._defaults(), options=True),
            )
        return self._save({CONF_FORECAST_SOURCE: source, **user_input}, SOLAR_KEYS)

    async def async_step_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="loads", data_schema=loads_schema(self._defaults(), options=True)
            )
        return self._save(user_input, LOADS_KEYS)

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="ev", data_schema=ev_schema(self._defaults(), options=True)
            )
        return self._save(user_input, EV_KEYS)

    async def async_step_hotwater(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="hotwater", data_schema=hotwater_schema(self._defaults(), options=True)
            )
        return self._save(user_input, HOTWATER_KEYS)

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="grid", data_schema=grid_schema(self._defaults(), options=True)
            )
        return self._save(user_input, GRID_KEYS)

    async def async_step_behaviour(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="behaviour", data_schema=behaviour_schema(self._defaults(), options=True)
            )
        return self._save(user_input, BEHAVIOUR_KEYS)
