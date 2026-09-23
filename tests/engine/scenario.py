"""Synthetic install used by every engine scenario (no real yard data)."""
from __future__ import annotations

from tests.engine.world import FakeWorld

T_PLAN = "2026-07-01 23:00:00"
ZONES = ("front", "back")


def zone_data(key: str, geography: str) -> dict:
    return {
        "key": key, "rachio_switch": f"switch.{key}_zone",
        "dominant_sensor": f"sensor.{key}_dominant",
        "state_sensor": f"sensor.{key}_state",
        "quality_sensors": [f"sensor.{key}_q1", f"sensor.{key}_q2", f"sensor.{key}_q3"],
        "target_range": "moist", "geography": geography, "adjacency": [],
        "runtime_minutes": 40, "rachio_zone_id": f"id-{key}",
        "refill_depth_mm": 7.5, "spray": False,
    }


def entry_data(*, self_cal: bool = False, overrides: str = "") -> dict:
    return {
        "bindings": {
            "notify_service": "notify.phone",
            "calendar_entity": "calendar.lawn",
            "rachio_device_name": "Main House",
            "rachio_api_key_secret": "rachio_api_key",
            "drought_level_select": "select.geodrops_rachio_drought_level",
            "standby_boolean": "switch.geodrops_rachio_standby",
            "standby_switch": "switch.rachio_standby",
            "dew_formed_boolean": "switch.geodrops_rachio_dew_formed",
            "run_active_boolean": "switch.geodrops_rachio_run_active",
        },
        "zones": [zone_data("front", "front"), zone_data("back", "back")],
        "self_calibration_enabled": self_cal,
        "advanced_overrides": overrides,
    }


def populate(world: FakeWorld, data: dict, *, level: str = "Level 1 - Mild",
             dominant: str = "60.0") -> None:
    """Every entity the scheduler reads, in a normal dry-night state."""
    b = data["bindings"]
    for z in data["zones"]:
        world.rachio.zone_switches.add(z["rachio_switch"])
        world.set(z["dominant_sensor"], dominant)
        world.set(z["state_sensor"], "Dry")
        for q in z["quality_sensors"]:
            world.set(q, "Good")
        world.set(f"switch.geodrops_rachio_{z['key']}_exclude", "off")
    world.set(b["drought_level_select"], level)
    world.set(b["standby_boolean"], "off")
    world.set(b["standby_switch"], "off")
    world.set(b["dew_formed_boolean"], "off")
    world.set(b["run_active_boolean"], "off")
    # Weather (brain defaults: Tempest entity ids).
    world.set("sensor.tempest_sensor_temperature", "60")
    world.set("sensor.tempest_sensor_humidity", "70")
    world.set("sensor.tempest_sensor_wind_speed_average", "5")
    world.set("sensor.tempest_rain_last_hour", "0")
    world.set("sensor.tempest_sensor_precipitation_type", "none")
    world.set("sensor.tempest_precipitation_today", "0")
    world.set("sensor.sun_next_dawn", "2026-07-02T04:30:00+00:00")
    world.set("sensor.sun_next_rising", "2026-07-02T05:00:00+00:00")
    for kind in ("forecast", "observed"):
        world.set(f"sensor.{kind}_overnight_temp", "60")
        world.set(f"sensor.{kind}_overnight_humidity", "80")
        world.set(f"sensor.{kind}_overnight_wind", "3")
    for h in (12, 18, 24):
        world.set(f"sensor.precipitation_chance_{h}_hour", "10")
        world.set(f"sensor.precipitation_amount_{h}_hour", "0")
