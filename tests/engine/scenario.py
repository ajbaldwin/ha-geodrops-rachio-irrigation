"""Synthetic install used by every engine scenario (no real yard data)."""
from __future__ import annotations

import asyncio
import copy

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.base import EngineBase
from custom_components.geodrops_rachio.engine.store import EngineStore
from tests.engine.world import FakePort, FakeWorld

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


async def _nosave(_docs):
    return None


def fake_fetch(world):
    async def fetch(_key_name):
        if world.rachio_api is None:
            raise RuntimeError("rachio api down")
        return copy.deepcopy(world.rachio_api)
    return fetch


def native_engine(world, data, *mixins, docs=None):
    cls = type("TestEngine", (*mixins, EngineBase), {})
    return cls(FakePort(world), EngineStore(docs or {}, _nosave),
               lambda: build_config(data), fake_fetch(world))


from custom_components.geodrops_rachio.engine.io import IOMixin  # noqa: E402
from custom_components.geodrops_rachio.engine.learning import LearningMixin  # noqa: E402
from custom_components.geodrops_rachio.engine.orchestration import OrchestrationMixin  # noqa: E402
from custom_components.geodrops_rachio.engine.planning import PlanningMixin  # noqa: E402
from custom_components.geodrops_rachio.engine.runner import RunnerMixin  # noqa: E402

ALL_MIXINS = (LearningMixin, OrchestrationMixin, PlanningMixin, RunnerMixin, IOMixin)

from custom_components.geodrops_rachio.engine.scheduler import Scheduler  # noqa: E402


def native_scheduler(world, data, docs=None):
    loop = asyncio.get_running_loop()
    return Scheduler(FakePort(world), EngineStore(docs or {}, _nosave),
                     lambda: build_config(data), fake_fetch(world),
                     lambda coro, name: loop.create_task(coro, name=name))
