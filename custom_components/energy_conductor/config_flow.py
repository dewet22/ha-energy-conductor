"""Config flow for the energy_conductor integration (spec §5).

Five required-or-optional steps walked in order:
  1. Battery (required)
  2. Tariff (required)
  3. Solar forecast source (required)
  4. Solar forecast details (required)
  5. EV charger (optional)
  6. Behaviour mode (required)
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
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_DEVICE_NAME,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_HOME_LOAD_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_MIN_TARGET_SOC_PERCENT,
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
    DEFAULT_MIN_TARGET_SOC_PERCENT,
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


BATTERY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BATTERY_SOC_SENSOR): _sensor_selector(device_class="battery"),
        vol.Required(CONF_BATTERY_CHARGE_CONTROL): _number_entity_selector(),
        vol.Required(CONF_BATTERY_DISCHARGE_LIMIT): _number_entity_selector(),
        vol.Required(CONF_BATTERY_CAPACITY_KWH): _kwh_selector(min_value=0.5, max_value=100),
        vol.Required(
            CONF_BATTERY_RESERVE_PERCENT, default=DEFAULT_RESERVE_PERCENT
        ): _percent_selector(),
        vol.Optional(CONF_RESERVE_SOC_SENSOR): _soc_floor_selector(),
    }
)

TARIFF_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OFF_PEAK_SENSOR): _binary_sensor_selector(),
        vol.Optional(CONF_DISPATCHING_SENSOR): _binary_sensor_selector(),
        vol.Required(
            CONF_OVERNIGHT_WINDOW_END_TIME,
            default=DEFAULT_OVERNIGHT_WINDOW_END_TIME.isoformat(),
        ): TimeSelector(),
    }
)


def _forecast_schema(source: str) -> vol.Schema:
    base: dict = {
        vol.Optional(CONF_SOLAR_GENERATION_SENSOR): _sensor_selector(),
        vol.Optional(CONF_HOME_LOAD_SENSOR): _sensor_selector(device_class="power"),
        vol.Optional(CONF_MANAGED_LOAD_SENSORS): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
        ),
        vol.Required(CONF_WINTER_MIN_KWH, default=DEFAULT_WINTER_MIN_KWH): _kwh_selector(
            max_value=20
        ),
        vol.Required(CONF_SUMMER_MAX_KWH, default=DEFAULT_SUMMER_MAX_KWH): _kwh_selector(
            max_value=50
        ),
        vol.Required(CONF_SOUTHERN_HEMISPHERE, default=False): BooleanSelector(),
    }
    if source == FORECAST_SOURCE_SOLCAST:
        base[vol.Required(CONF_FORECAST_SOLCAST_SENSOR)] = _sensor_selector()
    elif source == FORECAST_SOURCE_DAILY:
        base[vol.Required(CONF_FORECAST_DAILY_SENSOR)] = _sensor_selector()
    return vol.Schema(base)


FORECAST_SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FORECAST_SOURCE, default=FORECAST_SOURCE_SOLCAST): SelectSelector(
            SelectSelectorConfig(
                options=[FORECAST_SOURCE_SOLCAST, FORECAST_SOURCE_DAILY, FORECAST_SOURCE_NONE],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="forecast_source",
            )
        ),
    }
)

EV_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EV_POWER_SENSOR): _sensor_selector(),
        vol.Optional(
            CONF_EV_MIN_ACTIVATION_W, default=DEFAULT_EV_MIN_ACTIVATION_W
        ): _watts_selector(min_value=100, max_value=11000),
    }
)

BEHAVIOUR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WRITE_MODE, default=WRITE_MODE_DRY_RUN): SelectSelector(
            SelectSelectorConfig(
                options=[WRITE_MODE_DRY_RUN, WRITE_MODE_LIVE],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="write_mode",
            )
        ),
        vol.Required(CONF_NOTIFY_TARGET): EntitySelector(EntitySelectorConfig(domain="notify")),
        vol.Required(
            CONF_OVERNIGHT_PLAN_TIME, default=DEFAULT_OVERNIGHT_PLAN_TIME.isoformat()
        ): TimeSelector(),
        vol.Required(CONF_DAILY_KWH_TARGET, default=DEFAULT_DAILY_KWH_TARGET): _kwh_selector(
            max_value=200
        ),
        vol.Required(
            CONF_MIN_TARGET_SOC_PERCENT, default=DEFAULT_MIN_TARGET_SOC_PERCENT
        ): _percent_selector(),
    }
)


class EnergyConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_battery(user_input)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="battery", data_schema=BATTERY_SCHEMA)
        self._data.update(user_input)
        return await self.async_step_tariff()

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="tariff", data_schema=TARIFF_SCHEMA)
        self._data.update(user_input)
        return await self.async_step_forecast_source()

    async def async_step_forecast_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="forecast_source", data_schema=FORECAST_SOURCE_SCHEMA
            )
        self._data.update(user_input)
        return await self.async_step_forecast()

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        source = self._data[CONF_FORECAST_SOURCE]
        schema = _forecast_schema(source)
        if user_input is None:
            return self.async_show_form(step_id="forecast", data_schema=schema)
        self._data.update(user_input)
        return await self.async_step_ev()

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="ev", data_schema=EV_SCHEMA)
        # Only store EV fields when a power sensor is actually configured
        if user_input.get(CONF_EV_POWER_SENSOR):
            self._data.update({k: v for k, v in user_input.items() if v is not None})
        return await self.async_step_behaviour()

    async def async_step_behaviour(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="behaviour", data_schema=BEHAVIOUR_SCHEMA)
        self._data.update(user_input)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Energy Conductor", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyConductorOptionsFlow()


class EnergyConductorOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        # In HA 2026.5+, OptionsFlow.config_entry is a read-only property set by the
        # framework before __init__ runs. Access it via self.config_entry in step handlers.
        pass

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_behaviour(user_input)

    async def async_step_behaviour(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Merge data+options here (not in __init__) because self.config_entry is
        # injected by the framework after __init__ runs in HA 2026.5+.
        defaults: dict[str, Any] = {
            **self.config_entry.data,
            **self.config_entry.options,
        }
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_WRITE_MODE,
                        default=defaults.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[WRITE_MODE_DRY_RUN, WRITE_MODE_LIVE],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="write_mode",
                        )
                    ),
                    vol.Required(
                        CONF_NOTIFY_TARGET,
                        default=defaults.get(CONF_NOTIFY_TARGET, ""),
                    ): EntitySelector(EntitySelectorConfig(domain="notify")),
                    vol.Required(
                        CONF_DAILY_KWH_TARGET,
                        default=defaults.get(CONF_DAILY_KWH_TARGET, DEFAULT_DAILY_KWH_TARGET),
                    ): _kwh_selector(max_value=200),
                    vol.Required(
                        CONF_MIN_TARGET_SOC_PERCENT,
                        default=defaults.get(
                            CONF_MIN_TARGET_SOC_PERCENT, DEFAULT_MIN_TARGET_SOC_PERCENT
                        ),
                    ): _percent_selector(),
                    vol.Optional(
                        CONF_DEVICE_NAME,
                        description={"suggested_value": defaults.get(CONF_DEVICE_NAME, "")},
                    ): TextSelector(TextSelectorConfig()),
                    vol.Optional(
                        CONF_HOME_LOAD_SENSOR,
                        description={"suggested_value": defaults.get(CONF_HOME_LOAD_SENSOR)},
                    ): _sensor_selector(device_class="power"),
                    vol.Optional(
                        CONF_MANAGED_LOAD_SENSORS,
                        description={"suggested_value": defaults.get(CONF_MANAGED_LOAD_SENSORS)},
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
                    ),
                    vol.Optional(
                        CONF_FORECAST_SOLCAST_SENSOR,
                        description={"suggested_value": defaults.get(CONF_FORECAST_SOLCAST_SENSOR)},
                    ): _sensor_selector(),
                    # Battery entities/values are set in the initial flow (entry.data),
                    # but surfaced here too so they can be corrected post-setup without
                    # remove/re-add. Options override data in the coordinator's merged config.
                    vol.Optional(
                        CONF_BATTERY_CAPACITY_KWH,
                        description={"suggested_value": defaults.get(CONF_BATTERY_CAPACITY_KWH)},
                    ): _kwh_selector(min_value=0.5, max_value=100),
                    vol.Optional(
                        CONF_BATTERY_CHARGE_CONTROL,
                        description={"suggested_value": defaults.get(CONF_BATTERY_CHARGE_CONTROL)},
                    ): _number_entity_selector(),
                    vol.Optional(
                        CONF_BATTERY_DISCHARGE_LIMIT,
                        description={"suggested_value": defaults.get(CONF_BATTERY_DISCHARGE_LIMIT)},
                    ): _number_entity_selector(),
                    vol.Optional(
                        CONF_RESERVE_SOC_SENSOR,
                        description={"suggested_value": defaults.get(CONF_RESERVE_SOC_SENSOR)},
                    ): _soc_floor_selector(),
                    # Forecast seasonal-fallback bounds — also act as the plausibility
                    # ceiling for Solcast (summer_max x 1.5). The initial flow sets 8.0
                    # as the default; expose here so the user can correct it post-setup
                    # without knowing to re-run the full wizard.
                    vol.Optional(
                        CONF_SUMMER_MAX_KWH,
                        description={"suggested_value": defaults.get(CONF_SUMMER_MAX_KWH)},
                    ): _kwh_selector(max_value=100),
                    vol.Optional(
                        CONF_WINTER_MIN_KWH,
                        description={"suggested_value": defaults.get(CONF_WINTER_MIN_KWH)},
                    ): _kwh_selector(max_value=50),
                }
            )
            return self.async_show_form(step_id="behaviour", data_schema=schema)
        # Persist behaviour keys plus any battery corrections. Battery fields are
        # only written when supplied (preserving entry.data when left blank).
        persisted: dict[str, Any] = {
            CONF_WRITE_MODE: user_input[CONF_WRITE_MODE],
            CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
            CONF_DAILY_KWH_TARGET: user_input[CONF_DAILY_KWH_TARGET],
            CONF_MIN_TARGET_SOC_PERCENT: user_input[CONF_MIN_TARGET_SOC_PERCENT],
            CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME) or None,
            CONF_HOME_LOAD_SENSOR: user_input.get(CONF_HOME_LOAD_SENSOR) or None,
            CONF_MANAGED_LOAD_SENSORS: user_input.get(CONF_MANAGED_LOAD_SENSORS) or [],
            CONF_FORECAST_SOLCAST_SENSOR: user_input.get(CONF_FORECAST_SOLCAST_SENSOR) or None,
            CONF_RESERVE_SOC_SENSOR: user_input.get(CONF_RESERVE_SOC_SENSOR) or None,
        }
        for key in (
            CONF_BATTERY_CAPACITY_KWH,
            CONF_BATTERY_CHARGE_CONTROL,
            CONF_BATTERY_DISCHARGE_LIMIT,
            CONF_SUMMER_MAX_KWH,
            CONF_WINTER_MIN_KWH,
        ):
            if user_input.get(key) is not None:
                persisted[key] = user_input[key]
        return self.async_create_entry(title="", data=persisted)
