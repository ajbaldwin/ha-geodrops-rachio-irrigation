"""Config flow (setup wizard) and options flow for GeoDrops + Rachio.

The wizard COLLECTS external Home Assistant entities from the user, then
ASSEMBLES the complete `bindings` dict that `config_writer.build_config`
(frozen) places verbatim in the scheduler config's `homeassistant:`
section. That section must match the scheduler's real `HABindings` /
`parse_bindings` schema, so the assembled dict also folds in:
  - fixed, integration-owned entity ids (native entities + derived sensors
    created by Task 6c), and
  - safe HA defaults for the sun anchors that are never asked in the wizard.

See: .superpowers/sdd/2026-09-12-ha-integration-wrapper/task-5-addendum.md
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, selector

from .const import DOMAIN, SETTINGS_KEYS
from .config_writer import build_config, entry_config, split_settings
from .entity_renames import replace_entity_id
from .rachio_client import (
    RachioAuthError,
    RachioConnectionError,
    async_fetch_account,
    async_fetch_device_zones,
)
from .util import slug

_LOGGER = logging.getLogger(__name__)

REQUIRED_COMPONENTS = ("rachio",)

# Sentinel option in the Rachio-zone picker for a zone that isn't in Rachio
# (e.g. a hose/spray zone) or when the user prefers to type the numbers.
MANUAL_ZONE = "__manual__"

# notify.* services that can't be driven by `service.call("notify", <name>)`
# the way the scheduler does — the generic entity-targeting service is excluded
# from the notify picker and rejected by validation.
GENERIC_NOTIFY_SERVICES = {"send_message"}

# ---- Fixed, integration-owned bindings (native entities from Task 6c) ----
FIXED_BINDINGS: dict[str, Any] = {
    "drought_level_select": "select.geodrops_rachio_drought_level",
    "standby_boolean": "switch.geodrops_rachio_standby",
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
DEFAULT_WEATHER: dict[str, str] = {
    "temperature": "sensor.tempest_sensor_temperature",
    "humidity": "sensor.tempest_sensor_humidity",
    "wind": "sensor.tempest_sensor_wind_speed_average",
    "rain_last_hour": "sensor.tempest_rain_last_hour",
    "precip_type": "sensor.tempest_sensor_precipitation_type",
    "rain_today": "sensor.tempest_precipitation_today",
}
DEFAULT_PRECIPITATION_CHANCE_PREFIX = "sensor.precipitation_chance_"
DEFAULT_PRECIPITATION_AMOUNT_PREFIX = "sensor.precipitation_amount_"


def _api_key_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_API_KEY): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))})


def _prereqs_met(hass) -> bool:
    return all(c in hass.config.components for c in REQUIRED_COMPONENTS)


def _zone_label(zone: dict) -> str:
    """Human label for a live Rachio zone in the picker dropdown."""
    name = zone.get("name") or "Zone"
    num = zone.get("zoneNumber")
    label = f"{name} — #{num}" if num is not None else name
    runtime = zone.get("runtime_minutes")
    if runtime is not None:
        label += f" ({runtime:g} min)"
    return label


def _slug(name: str) -> str:
    """A config-key-friendly slug from a Rachio zone name (editable default)."""
    return slug(name)


def _optional_number(name: str, default):
    """Voluptuous key that pre-fills a number when known, else requires entry."""
    if default is None:
        return vol.Required(name)
    return vol.Optional(name, default=default)


def _assemble_bindings(core: dict[str, Any], weather: dict[str, Any]) -> dict[str, Any]:
    """Merge user-collected fields with fixed/default constants.

    Produces the complete `HABindings`-shaped dict per the Task 5 addendum.
    `core` is the output of the `bindings` step; `weather` is the output of
    the `weather` step.
    """
    return {
        "notify_service": core["notify_service"],
        "calendar_entity": core["calendar_entity"],
        "rachio_device_name": core["rachio_device_name"],
        "standby_switch": core["standby_switch"],
        "forecast_entity": core["forecast_entity"],
        "weather": {
            "temperature": weather["weather_temperature"],
            "humidity": weather["weather_humidity"],
            "wind": weather["weather_wind"],
            "rain_last_hour": weather["weather_rain_last_hour"],
            "precip_type": weather["weather_precip_type"],
            "rain_today": weather["weather_rain_today"],
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
    # The options flow has the manage-zones menu as its hub; the first-install
    # config flow does not. Adding a zone returns to that menu in the options
    # flow, and uses the add-another/advanced path in the config flow.
    _is_options: bool = False

    def _existing_bindings(self) -> dict[str, Any]:
        return dict(self._existing.get("bindings", {}))

    def _persist(self) -> None:
        """Options-only: write the in-progress _data to the config entry now,
        the settings to its options and the rest to its data (the settings'
        pre-1.3 copies in the data are kept for a rollback).

        The reload listener is suppressed while the dialog is open (see
        __init__._reload_on_options), so this saves each edit durably WITHOUT
        restarting the scheduler mid-flow. The single reload happens on "Done".
        """
        # An entity renamed while the dialog is open was rewritten in the
        # entry, not in this flow's copy; carry the rename over rather than
        # write the old id back (see entity_renames).
        runtime = getattr(self.config_entry, "runtime_data", None)
        for old, new in (runtime.renamed if runtime else {}).items():
            self._data = replace_entity_id(self._data, old, new)
        entry = self.config_entry
        data, options = split_settings(self._data)
        data.update({k: entry.data[k] for k in SETTINGS_KEYS if k in entry.data})
        self.hass.config_entries.async_update_entry(
            entry, data=data, options={**entry.options, **options})

    def _core_from_bindings(self) -> dict[str, Any]:
        """The Core-step field set, read back out of _data['bindings'].

        Lets the Weather sub-step re-assemble a complete bindings dict from the
        existing core without the admin re-walking Core.
        """
        b = self._data.get("bindings", {})
        return {
            "notify_service": b.get("notify_service"),
            "calendar_entity": b.get("calendar_entity"),
            "rachio_device_name": b.get("rachio_device_name"),
            "standby_switch": b.get("standby_switch"),
            "forecast_entity": b.get("forecast_entity"),
        }

    def _weather_form_from_bindings(self) -> dict[str, Any]:
        """The Weather-step form dict, reconstructed from _data['bindings'], so
        the Core sub-step can re-assemble a complete bindings dict without the
        admin re-walking Weather."""
        b = self._data.get("bindings", {})
        w = b.get("weather", {})
        d = b.get("derived", {})
        return {
            "weather_temperature": w.get("temperature", DEFAULT_WEATHER["temperature"]),
            "weather_humidity": w.get("humidity", DEFAULT_WEATHER["humidity"]),
            "weather_wind": w.get("wind", DEFAULT_WEATHER["wind"]),
            "weather_rain_last_hour": w.get(
                "rain_last_hour", DEFAULT_WEATHER["rain_last_hour"]),
            "weather_precip_type": w.get(
                "precip_type", DEFAULT_WEATHER["precip_type"]),
            "weather_rain_today": w.get(
                "rain_today", DEFAULT_WEATHER["rain_today"]),
            "precipitation_chance_prefix": d.get(
                "precipitation_chance_prefix", DEFAULT_PRECIPITATION_CHANCE_PREFIX),
            "precipitation_amount_prefix": d.get(
                "precipitation_amount_prefix", DEFAULT_PRECIPITATION_AMOUNT_PREFIX),
        }

    async def _connect_rachio(self, key: str) -> str | None:
        """Check API key `key` with Rachio and fetch the account's controllers.

        Returns the form error (`invalid_auth`, `cannot_connect`) or None. On
        success `_api_key`, `_account_id` and `_devices` are set, so the
        device-name field can be a live dropdown and zones can pre-fill.
        """
        self._devices = []
        self._api_key = None
        self._account_id = None
        session = aiohttp_client.async_get_clientsession(self.hass)
        try:
            self._account_id, self._devices = await async_fetch_account(session, key)
        except RachioAuthError:
            return "invalid_auth"
        except RachioConnectionError as err:
            _LOGGER.debug("Rachio account check failed: %s", err)
            return "cannot_connect"
        self._api_key = key
        return None

    async def async_step_connect(self, user_input=None):
        """Collect and check the Rachio API key.

        Runs first so the device-name field can be a live dropdown of the user's
        controllers and the zone step can pre-fill from Rachio.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            error = await self._connect_rachio(key)
            if error:
                errors["base"] = error
            elif self._is_options:
                entry = self.config_entry
                if entry.unique_id not in (None, self._account_id):
                    errors["base"] = "wrong_account"
                else:
                    if entry.unique_id is None:
                        self.hass.config_entries.async_update_entry(
                            entry, unique_id=self._account_id)
                    self._data[CONF_API_KEY] = key
                    self._persist()
                    return await self.async_step_menu()
            else:
                await self.async_set_unique_id(self._account_id)
                self._abort_if_unique_id_configured()
                self._data[CONF_API_KEY] = key
                return await self.async_step_bindings()
        return self.async_show_form(
            step_id="connect", data_schema=_api_key_schema(), errors=errors)

    def _device_name_field(self, default):
        """(key, selector) for device name: a dropdown of fetched controllers,
        or a free-text field when none were fetched."""
        if self._devices:
            names = [d["name"] for d in self._devices if d.get("name")]
            if default not in names and names:
                default = names[0]
            return (
                vol.Required("rachio_device_name", default=default),
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=names, mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=True)),
            )
        return (vol.Required("rachio_device_name", default=default or ""), str)

    async def _fetch_zones_for_device(self, device_name: str) -> None:
        """Fetch the chosen controller's zones for the picker; empty on any
        failure so the zone step falls back to the manual form."""
        self._live_zones = []
        if not self._api_key:
            return
        device = next(
            (d for d in self._devices if d.get("name") == device_name), None)
        if not device:
            return
        session = aiohttp_client.async_get_clientsession(self.hass)
        try:
            self._live_zones = await async_fetch_device_zones(
                session, self._api_key, device["id"])
        except Exception:  # noqa: BLE001 - degrade to manual zone entry
            _LOGGER.debug(
                "Rachio zone fetch failed; using manual zone entry",
                exc_info=True,
            )
            self._live_zones = []

    def _notify_options(self) -> list[str]:
        """Callable notify.* services, as `notify.<name>` values.

        The scheduler calls `service.call("notify", <name>)`, so the target must
        be a real notify service (e.g. a legacy notify.mobile_app_*), NOT a
        notify entity that only answers notify.send_message. The generic
        send_message service needs an entity_id target, so it's excluded.
        """
        services = self.hass.services.async_services().get("notify", {})
        return sorted(
            f"notify.{name}" for name in services
            if name not in GENERIC_NOTIFY_SERVICES)

    def _notify_service_field(self, default):
        options = self._notify_options()
        if options:
            if default not in options:
                default = options[0]
            return (
                vol.Required("notify_service", default=default),
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=True)),
            )
        # No notify services registered (unusual) — accept free text so setup
        # isn't blocked; the submit-time check still validates it.
        return (vol.Required("notify_service", default=default or ""), str)

    def _notify_service_valid(self, value: str) -> bool:
        name = (value or "").split(".", 1)[-1]
        return bool(name) and name not in GENERIC_NOTIFY_SERVICES and (
            self.hass.services.has_service("notify", name))

    def _build_bindings_schema(self, defaults: dict) -> vol.Schema:
        notify_key, notify_selector = self._notify_service_field(
            defaults.get("notify_service"))
        device_key, device_selector = self._device_name_field(
            defaults.get("rachio_device_name", ""))
        return vol.Schema({
            notify_key: notify_selector,
            vol.Required(
                "calendar_entity", default=defaults.get("calendar_entity")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar")),
            device_key: device_selector,
            vol.Optional(
                "standby_switch",
                default=defaults.get("standby_switch", DEFAULT_STANDBY_SWITCH),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
            vol.Required(
                "forecast_entity", default=defaults.get("forecast_entity")
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        })

    async def async_step_bindings(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if not self._notify_service_valid(user_input["notify_service"]):
                errors["notify_service"] = "invalid_notify_service"
            else:
                self._core = {
                    "notify_service": user_input["notify_service"],
                    "calendar_entity": user_input["calendar_entity"],
                    "rachio_device_name": user_input["rachio_device_name"],
                    "standby_switch": user_input["standby_switch"],
                    "forecast_entity": user_input["forecast_entity"],
                }
                await self._fetch_zones_for_device(user_input["rachio_device_name"])
                if self._is_options:
                    self._data["bindings"] = _assemble_bindings(
                        self._core, self._weather_form_from_bindings())
                    self._persist()
                    return await self.async_step_menu()
                return await self.async_step_weather()

        defaults = user_input if user_input is not None else self._existing_bindings()
        schema = self._build_bindings_schema(defaults)
        return self.async_show_form(
            step_id="bindings", data_schema=schema, errors=errors)

    async def async_step_weather(self, user_input=None):
        if user_input is not None:
            if self._is_options:
                self._data["bindings"] = _assemble_bindings(
                    self._core_from_bindings(), user_input)
                self._persist()
                return await self.async_step_menu()
            self._data["bindings"] = _assemble_bindings(self._core, user_input)
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
                "weather_rain_today",
                default=existing_weather.get(
                    "rain_today", DEFAULT_WEATHER["rain_today"]),
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

    async def async_step_menu(self, user_input=None):
        """Options hub. Every sub-step returns here; 'Done' saves and reloads.
        The labels are translated (options.step.menu.menu_options)."""
        return self.async_show_menu(
            step_id="menu",
            menu_options=["connect", "bindings", "weather", "add_zone",
                          "edit_zone", "remove_zone", "advanced", "finish"])

    async def async_step_add_zone(self, user_input=None):
        self._editing_key = None
        return await self.async_step_zone()

    async def async_step_finish(self, user_input=None):
        # Options-hub "Done": persist, drop the reload guard, restart once.
        return await self._async_finish()

    async def async_step_edit_zone(self, user_input=None):
        self._removing = False
        return await self.async_step_pick_zone()

    async def async_step_remove_zone(self, user_input=None):
        self._removing = True
        return await self.async_step_pick_zone()

    async def async_step_pick_zone(self, user_input=None):
        keys = [z["key"] for z in self._data["zones"]]
        if user_input is not None:
            self._selected_key = user_input["zone"]
            if self._removing:
                return await self.async_step_confirm_remove()
            self._editing_key = self._selected_key
            self._picked_zone = None
            return await self.async_step_zone_details()
        schema = vol.Schema({vol.Required("zone"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=keys,
                                          mode=selector.SelectSelectorMode.DROPDOWN))})
        return self.async_show_form(step_id="pick_zone", data_schema=schema)

    async def async_step_confirm_remove(self, user_input=None):
        if user_input is not None:
            if user_input.get("confirm"):
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._selected_key]
                self._persist()
            return await self.async_step_menu()
        schema = vol.Schema({vol.Required("confirm", default=False): bool})
        return self.async_show_form(step_id="confirm_remove", data_schema=schema)

    def _stored_zone(self, key):
        return next((z for z in self._data["zones"] if z["key"] == key), {})

    def _rachio_switch_selector(self):
        """Switch picker constrained to the Rachio integration's entities."""
        return selector.EntitySelector(selector.EntitySelectorConfig(
            domain="switch", integration="rachio"))

    def _target_range_selector(self):
        return selector.SelectSelector(
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
            ))

    def _append_zone(self, user_input, *, rachio_zone_id: str = "") -> None:
        zone = {
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
        }
        # Only emit rachio_zone_id when we actually captured one, so the manual
        # path's generated config stays identical to before this feature.
        if rachio_zone_id:
            zone["rachio_zone_id"] = rachio_zone_id
        self._data["zones"].append(zone)

    async def async_step_zone(self, user_input=None):
        """Pick a zone from the live Rachio list, or fall back to manual entry.

        When the wizard could not reach Rachio (`_live_zones` empty) this is the
        original single manual form. When it could, this is a picker whose
        selection pre-fills the follow-up `zone_details` step.
        """
        live = getattr(self, "_live_zones", [])
        if not live:
            return await self._async_step_zone_manual(user_input)

        if user_input is not None:
            picked = user_input["rachio_zone"]
            self._picked_zone = next(
                (z for z in live if z["id"] == picked), None)
            return await self.async_step_zone_details()

        options = [
            {"value": z["id"], "label": _zone_label(z)} for z in live
        ]
        options.append(
            {"value": MANUAL_ZONE, "label": "Enter manually / not a Rachio zone"})
        schema = vol.Schema({
            vol.Required("rachio_zone", default=options[0]["value"]):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN)),
        })
        return self.async_show_form(step_id="zone", data_schema=schema)

    def _guess_switch(self, zone_name: str):
        """Best-guess HA switch entity for a Rachio zone, matched by name.

        The Rachio integration names its zone switches after the zone (e.g.
        "Front Yard" -> switch.front_yard). Prefer a friendly-name match, then
        an exact object-id match, then an object-id containing the slug. Returns
        None when nothing plausible exists, so the field then requires a pick.
        """
        slug = _slug(zone_name)
        if not slug:
            return None
        states = self.hass.states.async_all("switch")
        # Friendly-name match, normalized so "Front Slope" == "front_slope".
        for st in states:
            friendly = st.attributes.get("friendly_name")
            if friendly and _slug(friendly) == slug:
                return st.entity_id
        ids = [st.entity_id for st in states]
        exact = f"switch.{slug}"
        if exact in ids:
            return exact
        for eid in ids:
            if slug in eid.split(".", 1)[1]:
                return eid
        return None

    async def async_step_zone_details(self, user_input=None):
        """Collect a zone's entities, with runtime/refill/key pre-filled from
        the picked Rachio zone (all still editable).

        When editing an existing zone (`self._editing_key` set), every field
        instead pre-fills from the stored zone, the key stays immutable (no
        key field is shown), and submit replaces that zone in place.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._editing_key:
                stored = self._stored_zone(self._editing_key)
                zid = (
                    self._picked_zone["id"] if self._picked_zone
                    else stored.get("rachio_zone_id", ""))
                self._data["zones"] = [
                    z for z in self._data["zones"] if z["key"] != self._editing_key]
                self._append_zone(dict(user_input, key=self._editing_key),
                                  rachio_zone_id=zid)
                self._editing_key = None
                self._persist()
                return await self.async_step_menu()
            if any(slug(user_input["key"]) == slug(z["key"])
                   for z in self._data["zones"]):
                errors["key"] = "duplicate_zone_key"
            else:
                zid = self._picked_zone["id"] if self._picked_zone else ""
                self._append_zone(user_input, rachio_zone_id=zid)
                if self._is_options:
                    # The hub is home base: adding returns there so the new zone
                    # is already persisted and the admin can add/edit/remove more.
                    self._persist()
                    return await self.async_step_menu()
                if user_input.get("add_another_zone"):
                    return await self.async_step_zone()
                return await self.async_step_advanced()

        editing = bool(self._editing_key)
        stored = self._stored_zone(self._editing_key) if editing else {}
        pz = self._picked_zone or {}
        existing_keys = [z["key"] for z in self._data["zones"]
                         if z["key"] != self._editing_key]
        runtime = stored.get("runtime_minutes") if editing else pz.get("runtime_minutes")
        refill = stored.get("refill_depth_mm") if editing else pz.get("refill_depth_mm")
        switch_guess = (
            stored.get("rachio_switch") if editing
            else self._guess_switch(pz.get("name", "")))
        switch_key = (
            vol.Optional("rachio_switch", default=switch_guess) if switch_guess
            else vol.Required("rachio_switch"))
        schema_dict: dict = {}
        if not editing:
            schema_dict[vol.Required("key", default=_slug(pz.get("name", "")))] = str
        schema_dict[switch_key] = self._rachio_switch_selector()
        schema_dict[
            vol.Required("dominant_sensor", default=stored.get("dominant_sensor"))
            if editing else vol.Required("dominant_sensor")
        ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        schema_dict[
            vol.Required("state_sensor", default=stored.get("state_sensor"))
            if editing else vol.Required("state_sensor")
        ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        schema_dict[
            vol.Required("quality_sensors", default=stored.get("quality_sensors"))
            if editing else vol.Required("quality_sensors")
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True))
        schema_dict[vol.Required(
            "target_range", default=stored.get("target_range", "moist"))
        ] = self._target_range_selector()
        schema_dict[vol.Optional(
            "geography", default=stored.get("geography", ""))] = str
        schema_dict[vol.Optional(
            "adjacency", default=stored.get("adjacency", []))
        ] = selector.SelectSelector(selector.SelectSelectorConfig(
            options=existing_keys, multiple=True, custom_value=True))
        schema_dict[_optional_number("runtime_minutes", runtime)] = vol.Coerce(float)
        schema_dict[_optional_number("refill_depth_mm", refill)] = vol.Coerce(float)
        schema_dict[vol.Optional("spray", default=stored.get("spray", False))] = bool
        if not editing and not self._is_options:
            schema_dict[vol.Optional("add_another_zone", default=False)] = bool
        schema = vol.Schema(schema_dict)
        # On a validation re-render (duplicate key), keep everything the user
        # already typed instead of resetting to the pre-fill defaults.
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="zone_details", data_schema=schema, errors=errors)

    async def _async_step_zone_manual(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if any(slug(user_input["key"]) == slug(z["key"])
                   for z in self._data["zones"]):
                errors["key"] = "duplicate_zone_key"
            else:
                self._append_zone(user_input)
                if self._is_options:
                    self._persist()
                    return await self.async_step_menu()
                if user_input.get("add_another_zone"):
                    return await self.async_step_zone()
                return await self.async_step_advanced()

        existing_keys = [z["key"] for z in self._data["zones"]]
        schema = {
            vol.Required("key"): str,
            vol.Required("rachio_switch"): self._rachio_switch_selector(),
            vol.Required("dominant_sensor"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")),
            vol.Required("state_sensor"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")),
            vol.Required("quality_sensors"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Required("target_range", default="moist"):
                self._target_range_selector(),
            vol.Optional("geography", default=""): str,
            vol.Optional("adjacency", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=existing_keys, multiple=True, custom_value=True)),
            vol.Required("runtime_minutes"): vol.Coerce(float),
            vol.Required("refill_depth_mm"): vol.Coerce(float),
            vol.Optional("spray", default=False): bool,
        }
        if not self._is_options:
            schema[vol.Optional("add_another_zone", default=False)] = bool
        data_schema = vol.Schema(schema)
        # On a validation re-render (duplicate key), keep the user's typed fields
        # rather than clearing the form.
        if user_input is not None:
            data_schema = self.add_suggested_values_to_schema(data_schema, user_input)
        return self.async_show_form(
            step_id="zone", data_schema=data_schema, errors=errors)

    async def async_step_advanced(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            calib = user_input["self_calibration_enabled"]
            overrides = user_input.get("advanced_overrides", "")
            if self._is_options:
                trial = dict(
                    self._data, self_calibration_enabled=calib,
                    advanced_overrides=overrides)
                try:
                    # build_config is the single source of override validation
                    # (raises ValueError on non-YAML / non-mapping overrides).
                    build_config(trial)
                except ValueError:
                    errors["advanced_overrides"] = "invalid_advanced_overrides"
                else:
                    self._data["self_calibration_enabled"] = calib
                    self._data["advanced_overrides"] = overrides
                    self._persist()
                    return await self.async_step_menu()
            else:
                self._data["self_calibration_enabled"] = calib
                self._data["advanced_overrides"] = overrides
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
        # On a validation re-render keep what the admin typed.
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="advanced", data_schema=schema, errors=errors)

    async def _async_finish(self):
        raise NotImplementedError


class GeodropsRachioConfigFlow(config_entries.ConfigFlow, _BindingsWizardSteps, domain=DOMAIN):
    VERSION = 1
    # 2: the Rachio API key is stored in the entry (was a secrets.yaml name).
    # 3: the settings (SETTINGS_KEYS) are in the entry's options.
    # Minor bumps so older releases can still load the entry (rollback).
    MINOR_VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {"zones": []}
        self._core: dict[str, Any] = {}
        self._existing: dict[str, Any] = {}
        self._live_zones: list[dict] = []
        self._picked_zone: dict | None = None
        self._devices: list[dict] = []
        self._api_key: str | None = None
        self._account_id: str | None = None
        self._editing_key: str | None = None
        self._selected_key: str | None = None
        self._removing: bool = False

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if not _prereqs_met(self.hass):
            return self.async_abort(reason="missing_prerequisites")
        return await self.async_step_connect()

    async def _async_finish(self):
        data, options = split_settings(self._data)
        return self.async_create_entry(
            title="GeoDrops + Rachio Irrigation", data=data, options=options)

    async def async_step_reauth(self, entry_data):
        """Rachio rejected the stored API key (or there is none yet)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._async_replace_key(
            "reauth_confirm", self._get_reauth_entry(), "reauth_successful",
            user_input)

    async def async_step_reconfigure(self, user_input=None):
        """Replace the Rachio API key by choice, e.g. after regenerating it.
        Everything else set up in the wizard is changed under Configure."""
        return await self._async_replace_key(
            "reconfigure", self._get_reconfigure_entry(), "reconfigure_successful",
            user_input)

    async def _async_replace_key(self, step_id: str,
                                 entry: config_entries.ConfigEntry,
                                 success: str, user_input):
        """Ask for a new API key for `entry`'s Rachio account and store it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            error = await self._connect_rachio(key)
            if error:
                errors["base"] = error
            else:
                if entry.unique_id not in (None, self._account_id):
                    return self.async_abort(reason="wrong_account")
                # The running scheduler reads the key from the entry on each
                # fetch, so a key change needs no reload (see __init__._snapshot).
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=self._account_id,
                    data={**entry.data, CONF_API_KEY: key})
                if entry.state is not ConfigEntryState.LOADED:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason=success)
        return self.async_show_form(
            step_id=step_id, data_schema=_api_key_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "GeodropsRachioOptionsFlow":
        return GeodropsRachioOptionsFlow()


class GeodropsRachioOptionsFlow(config_entries.OptionsFlow, _BindingsWizardSteps):
    """Re-runs the wizard's steps, pre-filled from the existing entry."""

    _is_options = True

    def __init__(self) -> None:
        self._data: dict[str, Any] = {"zones": []}
        self._core: dict[str, Any] = {}
        self._existing: dict[str, Any] = {}
        self._live_zones: list[dict] = []
        self._picked_zone: dict | None = None
        self._devices: list[dict] = []
        self._api_key: str | None = None
        self._account_id: str | None = None
        self._editing_key: str | None = None
        self._selected_key: str | None = None
        self._removing: bool = False
        self._guarding: bool = False

    async def async_step_init(self, user_input=None):
        self._existing = entry_config(
            self.config_entry.data, self.config_entry.options)
        # Seed _data fully so any early sub-step persists a COMPLETE entry — an
        # unopened section (bindings, zones, calib, overrides) is kept verbatim.
        self._data["bindings"] = dict(self._existing.get("bindings", {}))
        self._data["zones"] = list(self._existing.get("zones", []))
        self._data["self_calibration_enabled"] = self._existing.get(
            "self_calibration_enabled", False)
        self._data["advanced_overrides"] = self._existing.get("advanced_overrides", "")
        if CONF_API_KEY in self._existing:
            self._data[CONF_API_KEY] = self._existing[CONF_API_KEY]
        # Defer scheduler restarts until "Done" (see __init__._reload_on_options).
        # An entry that is not running has no reload listener to hold off.
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            runtime.suppress_reload = True
        self._guarding = True
        # Auto-connect with the stored key so the Core device dropdown and zone
        # pickers are live without visiting Connect. Best-effort: on failure
        # they fall back to free text and manual entry.
        if self._data.get(CONF_API_KEY):
            await self._connect_rachio(self._data[CONF_API_KEY])
        return await self.async_step_menu()

    async def _async_finish(self):
        entry = self.config_entry
        # Data is already persisted per-step; this is a no-op unless the admin
        # went straight to Done. Persist happens while the guard is still up.
        self._persist()
        self._release_guard()
        # Restart the scheduler only if something was actually edited: a reload
        # cancels a waiting or watering run (v0.9.x left the run alone too).
        from . import async_reload_if_changed
        await async_reload_if_changed(self.hass, entry)
        # The flow's result becomes the entry's options: the settings _persist
        # just wrote, so finishing changes nothing more.
        return self.async_create_entry(data=dict(entry.options))

    def _release_guard(self) -> bool:
        """Clear suppress_reload; returns whether this flow still held it."""
        held, self._guarding = self._guarding, False
        runtime = getattr(self.config_entry, "runtime_data", None)
        if held and runtime is not None:
            runtime.suppress_reload = False
        return held

    @callback
    def async_remove(self) -> None:
        """The flow ended. After "Done" there is nothing left to do; if the
        dialog was closed instead, the sub-steps' edits are already persisted,
        so apply them now rather than leave the scheduler on the old config
        with reloads suppressed until the next restart."""
        if self._release_guard():
            from . import async_reload_if_changed
            self.hass.async_create_task(
                async_reload_if_changed(self.hass, self.config_entry),
                "geodrops_rachio_options_closed_reload")
