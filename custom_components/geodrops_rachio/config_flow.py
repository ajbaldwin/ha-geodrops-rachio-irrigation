"""Config flow (setup wizard) and options flow for GeoDrops + Rachio.

The wizard COLLECTS external Home Assistant entities from the user, then
ASSEMBLES the complete `bindings` dict that `config_writer.generate_config`
(frozen) dumps verbatim into the generated `config.yaml`'s `homeassistant:`
section. That section must match the scheduler's real `HABindings` /
`parse_bindings` schema, so the assembled dict also folds in:
  - fixed, integration-owned entity ids (native entities + derived sensors
    created by Task 6c), and
  - safe HA defaults for the sun anchors that are never asked in the wizard.

See: .superpowers/sdd/2026-09-12-ha-integration-wrapper/task-5-addendum.md
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN

REQUIRED_COMPONENTS = ("pyscript", "rachio")

# ---- Fixed, integration-owned bindings (native entities from Task 6c) ----
FIXED_BINDINGS: dict[str, Any] = {
    "drought_level_select": "select.geodrops_rachio_drought_level",
    "standby_boolean": "switch.geodrops_rachio_standby",
    "dew_formed_boolean": "switch.geodrops_rachio_dew_formed",
    "run_active_boolean": "switch.geodrops_rachio_run_active",
}

# ---- Fixed, integration-owned derived sensors (Task 6c) ----
FIXED_DERIVED: dict[str, Any] = {
    "forecast_overnight": {
        "temp": "sensor.geodrops_rachio_forecast_overnight_temp",
        "humidity": "sensor.geodrops_rachio_forecast_overnight_humidity",
        "wind": "sensor.geodrops_rachio_forecast_overnight_wind",
    },
    "observed_overnight": {
        "temp": "sensor.geodrops_rachio_observed_overnight_temp",
        "humidity": "sensor.geodrops_rachio_observed_overnight_humidity",
        "wind": "sensor.geodrops_rachio_observed_overnight_wind",
    },
}

# ---- Safe HA defaults, never asked in the wizard ----
SUN_DEFAULTS: dict[str, str] = {
    "dawn": "sensor.sun_next_dawn",
    "sunrise": "sensor.sun_next_rising",
}

# ---- Defaults offered for user-collected fields ----
DEFAULT_STANDBY_SWITCH = "switch.sprinkler_standby"
DEFAULT_RACHIO_API_KEY_SECRET = "rachio_api_key"
DEFAULT_WEATHER: dict[str, str] = {
    "temperature": "sensor.tempest_sensor_temperature",
    "humidity": "sensor.tempest_sensor_humidity",
    "wind": "sensor.tempest_sensor_wind_speed_average",
    "rain_last_hour": "sensor.tempest_rain_last_hour",
    "precip_type": "sensor.tempest_sensor_precipitation_type",
}
DEFAULT_PRECIPITATION_CHANCE_PREFIX = "sensor.precipitation_chance_"
DEFAULT_PRECIPITATION_AMOUNT_PREFIX = "sensor.precipitation_amount_"


def _prereqs_met(hass) -> bool:
    return all(c in hass.config.components for c in REQUIRED_COMPONENTS)


def _assemble_bindings(core: dict[str, Any], weather: dict[str, Any]) -> dict[str, Any]:
    """Merge user-collected fields with fixed/default constants.

    Produces the complete `HABindings`-shaped dict per the Task 5 addendum.
    `core` is the output of the `bindings` step; `weather` is the output of
    the `weather` step.
    """
    return {
        "notify_service": core["notify_service"],
        "calendar_entity": core["calendar_entity"],
        "rachio_api_key_secret": core["rachio_api_key_secret"],
        "rachio_device_name": core["rachio_device_name"],
        "standby_switch": core["standby_switch"],
        "forecast_entity": core["forecast_entity"],
        "weather": {
            "temperature": weather["weather_temperature"],
            "humidity": weather["weather_humidity"],
            "wind": weather["weather_wind"],
            "rain_last_hour": weather["weather_rain_last_hour"],
            "precip_type": weather["weather_precip_type"],
        },
        "derived": {
            "precipitation_chance_prefix": weather["precipitation_chance_prefix"],
            "precipitation_amount_prefix": weather["precipitation_amount_prefix"],
            **FIXED_DERIVED,
        },
        **FIXED_BINDINGS,
        "sun": dict(SUN_DEFAULTS),
    }


class _BindingsWizardSteps:
    """Step implementations shared by the config flow and the options flow.

    Both flows walk the same `bindings -> weather -> zone(s) -> advanced`
    sequence. The only difference is where defaults come from (fixed
    constants for a fresh install vs. the existing entry for options) and how
    the final step is persisted (`_async_finish`, implemented per-flow).
    """

    hass: Any
    _data: dict[str, Any]
    _core: dict[str, Any]
    _existing: dict[str, Any]

    def _existing_bindings(self) -> dict[str, Any]:
        return dict(self._existing.get("bindings", {}))

    async def async_step_bindings(self, user_input=None):
        if user_input is not None:
            self._core = {
                "notify_service": user_input["notify_service"],
                "calendar_entity": user_input["calendar_entity"],
                "rachio_device_name": user_input["rachio_device_name"],
                "rachio_api_key_secret": user_input["rachio_api_key_secret"],
                "standby_switch": user_input["standby_switch"],
                "forecast_entity": user_input["forecast_entity"],
            }
            return await self.async_step_weather()

        existing = self._existing_bindings()
        schema = vol.Schema({
            vol.Required(
                "notify_service", default=existing.get("notify_service")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="notify")),
            vol.Required(
                "calendar_entity", default=existing.get("calendar_entity")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar")),
            vol.Required(
                "rachio_device_name", default=existing.get("rachio_device_name", "")
            ): str,
            vol.Required(
                "rachio_api_key_secret",
                default=existing.get(
                    "rachio_api_key_secret", DEFAULT_RACHIO_API_KEY_SECRET),
            ): str,
            vol.Optional(
                "standby_switch",
                default=existing.get("standby_switch", DEFAULT_STANDBY_SWITCH),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
            vol.Required(
                "forecast_entity", default=existing.get("forecast_entity")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        })
        return self.async_show_form(step_id="bindings", data_schema=schema)

    async def async_step_weather(self, user_input=None):
        if user_input is not None:
            self._data["bindings"] = _assemble_bindings(self._core, user_input)
            if self._data["zones"]:
                # Options flow re-entering with existing zones already seeded:
                # don't force adding another one, let the admin opt in.
                return await self.async_step_zone_gate()
            return await self.async_step_zone()

        existing = self._existing_bindings()
        existing_weather = existing.get("weather", {})
        existing_derived = existing.get("derived", {})
        schema = vol.Schema({
            vol.Required(
                "weather_temperature",
                default=existing_weather.get(
                    "temperature", DEFAULT_WEATHER["temperature"]),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "weather_humidity",
                default=existing_weather.get("humidity", DEFAULT_WEATHER["humidity"]),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "weather_wind",
                default=existing_weather.get("wind", DEFAULT_WEATHER["wind"]),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "weather_rain_last_hour",
                default=existing_weather.get(
                    "rain_last_hour", DEFAULT_WEATHER["rain_last_hour"]),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "weather_precip_type",
                default=existing_weather.get(
                    "precip_type", DEFAULT_WEATHER["precip_type"]),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "precipitation_chance_prefix",
                default=existing_derived.get(
                    "precipitation_chance_prefix",
                    DEFAULT_PRECIPITATION_CHANCE_PREFIX),
            ): str,
            vol.Required(
                "precipitation_amount_prefix",
                default=existing_derived.get(
                    "precipitation_amount_prefix",
                    DEFAULT_PRECIPITATION_AMOUNT_PREFIX),
            ): str,
        })
        return self.async_show_form(step_id="weather", data_schema=schema)

    async def async_step_zone_gate(self, user_input=None):
        """Only reached by the options flow when zones already exist.

        Lets the admin keep the existing zones untouched instead of being
        forced to add another one every time they open options.
        """
        if user_input is not None:
            if user_input["add_or_edit_zones"]:
                return await self.async_step_zone()
            return await self.async_step_advanced()

        schema = vol.Schema({
            vol.Optional("add_or_edit_zones", default=False): bool,
        })
        return self.async_show_form(step_id="zone_gate", data_schema=schema)

    async def async_step_zone(self, user_input=None):
        if user_input is not None:
            self._data["zones"].append({
                "key": user_input["key"],
                "rachio_switch": user_input["rachio_switch"],
                "dominant_sensor": user_input["dominant_sensor"],
                "state_sensor": user_input["state_sensor"],
                "quality_sensors": user_input["quality_sensors"],
                "target_range": user_input["target_range"],
                "geography": user_input.get("geography", ""),
                "adjacency": user_input.get("adjacency", []),
                "runtime_minutes": user_input["runtime_minutes"],
                "refill_depth_mm": user_input["refill_depth_mm"],
                "spray": user_input.get("spray", False),
            })
            if user_input.get("add_another_zone"):
                return await self.async_step_zone()
            return await self.async_step_advanced()

        existing_keys = [z["key"] for z in self._data["zones"]]
        schema = vol.Schema({
            vol.Required("key"): str,
            vol.Required("rachio_switch"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")),
            vol.Required("dominant_sensor"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")),
            vol.Required("state_sensor"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")),
            vol.Required("quality_sensors"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Required("target_range", default="moist"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "dry", "label": "Dry"},
                        {"value": "dry_plus", "label": "Dry+"},
                        {"value": "moist", "label": "Moist"},
                        {"value": "moist_plus", "label": "Moist+"},
                        {"value": "wet", "label": "Wet"},
                        {"value": "wet_plus", "label": "Wet+"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            vol.Optional("geography", default=""): str,
            vol.Optional("adjacency", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=existing_keys, multiple=True, custom_value=True)),
            vol.Required("runtime_minutes"): vol.Coerce(float),
            vol.Required("refill_depth_mm"): vol.Coerce(float),
            vol.Optional("spray", default=False): bool,
            vol.Optional("add_another_zone", default=False): bool,
        })
        return self.async_show_form(step_id="zone", data_schema=schema)

    async def async_step_advanced(self, user_input=None):
        if user_input is not None:
            self._data["self_calibration_enabled"] = user_input["self_calibration_enabled"]
            self._data["advanced_overrides"] = user_input.get("advanced_overrides", "")
            return await self._async_finish()

        schema = vol.Schema({
            vol.Optional(
                "self_calibration_enabled",
                default=self._existing.get("self_calibration_enabled", False),
            ): bool,
            vol.Optional(
                "advanced_overrides",
                default=self._existing.get("advanced_overrides", ""),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="advanced", data_schema=schema)

    async def _async_finish(self):
        raise NotImplementedError


class GeodropsRachioConfigFlow(config_entries.ConfigFlow, _BindingsWizardSteps, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {"zones": []}
        self._core: dict[str, Any] = {}
        self._existing: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if not _prereqs_met(self.hass):
            return self.async_abort(reason="missing_prerequisites")
        return await self.async_step_bindings()

    async def _async_finish(self):
        return self.async_create_entry(
            title="GeoDrops + Rachio Irrigation", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "GeodropsRachioOptionsFlow":
        return GeodropsRachioOptionsFlow()


class GeodropsRachioOptionsFlow(config_entries.OptionsFlow, _BindingsWizardSteps):
    """Re-runs the wizard's steps, pre-filled from the existing entry."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {"zones": []}
        self._core: dict[str, Any] = {}
        self._existing: dict[str, Any] = {}

    async def async_step_init(self, user_input=None):
        self._existing = dict(self.config_entry.data)
        # Seed with the existing zones so declining to add more preserves them.
        self._data["zones"] = list(self._existing.get("zones", []))
        return await self.async_step_bindings()

    async def _async_finish(self):
        self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
        return self.async_create_entry(title="", data={})
