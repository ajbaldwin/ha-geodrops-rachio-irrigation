# Options Flow Hub Refactor — Design

Date: 2026-09-21
Status: Approved for planning
Scope: `custom_components/geodrops_rachio/config_flow.py`, `__init__.py`,
`strings.json`, `translations/en.json`, tests.

## Problem

The options flow re-runs the whole first-install wizard in sequence:

```
init → connect → bindings (Core Setup) → weather (Weather Station)
     → manage_zones (menu) → finish → advanced → save
```

To change one zone, the admin is forced through **Connect Your Rachio
Account → Core Setup → Weather Station** first — three screens of
prefilled fields they don't want to touch — before the zone menu even
appears. Saving then routes awkwardly through the Advanced step.

Two problems to fix:

1. **Navigation:** every options edit walks the full linear wizard.
2. **Zone-save durability:** zone add/edit/remove mutate `_data["zones"]`
   in memory only. Nothing persists until `finish → advanced →
   _async_finish`. Abandon the dialog mid-edit and the zone work is lost.

## Goals

- Options flow lands directly on a **flat hub menu**. Connect, Core
  Setup, and Weather Station become menu items, chosen only when needed.
- Each hub sub-step returns to the hub.
- "Done" saves and exits directly (no forced Advanced detour).
- Zone (and every other) edit **persists to the config entry the instant
  it's made** — abandoning the dialog never loses committed work.
- First-install wizard behavior is **unchanged**.

## Non-goals

- No changes to the field sets, validation, Rachio polling, or generated
  `config.yaml` schema.
- No new zone-editing features (bulk edit, reorder, etc.).
- No change to the first-install linear wizard.

## Constraint that shapes the design: the reload listener

`__init__.py` registers `entry.add_update_listener(_reload_on_options)`.
Any `async_update_entry` that changes entry data triggers
`async_reload`, which re-runs `async_setup_entry`: regenerates
`config.yaml`, redelivers and restarts the pyscript app, stops/starts the
coordinator, re-forwards platforms, and purges orphan zone devices.

Naive persist-on-mutation would therefore **reload the whole scheduler
after every single zone add/edit/remove, while the dialog is still
open** — potentially interrupting a live irrigation evaluation. This is
unacceptable, so persistence during the flow must be **decoupled from the
reload**.

## Design

### 1. Hub menu (options flow only)

`async_step_init` (options) does setup work, then shows the hub via a new
`async_step_menu`:

1. Seed `_data` fully from the existing entry (see §2).
2. Enable reload suppression (see §4).
3. Auto-connect to Rachio using the stored secret (see §3).
4. `return await self.async_step_menu()`.

`async_step_menu` replaces the old `manage_zones` step. Flat menu:

| Menu item     | Handler                     | Returns to |
|---------------|-----------------------------|------------|
| `connect`     | `async_step_connect`        | menu       |
| `bindings`    | `async_step_bindings`       | menu       |
| `weather`     | `async_step_weather`        | menu       |
| `add_zone`    | `async_step_add_zone`       | menu       |
| `edit_zone`   | `async_step_edit_zone`      | menu       |
| `remove_zone` | `async_step_remove_zone`    | menu       |
| `advanced`    | `async_step_advanced`       | menu       |
| `finish`      | `_async_finish` (save+exit) | —          |

Menu labels stay inline dicts (as `manage_zones` already does) so the
menu never renders blank before translations load.

Retitle the step from "Manage Zones" to a hub title, e.g. **"GeoDrops +
Rachio Options"**.

### 2. Seed `_data` at init (required)

Today `_data` starts as `{"zones": [...]}`; `bindings` and the advanced
fields are only populated if the user walks those steps. With a hub where
any sub-step can persist the whole `_data`, an early zone edit would
persist **empty bindings**.

Fix: at `async_step_init`, copy from `self._existing`:

- `_data["bindings"] = self._existing.get("bindings", {})`
- `_data["zones"] = list(self._existing.get("zones", []))` (already done)
- `_data["self_calibration_enabled"] = self._existing.get(..., False)`
- `_data["advanced_overrides"] = self._existing.get("advanced_overrides", "")`

Any section the admin never opens is preserved verbatim.

Note: `async_step_bindings`/`async_step_weather` currently only assemble
`_data["bindings"]` together (weather submit calls `_assemble_bindings`).
In options, editing Core alone must not require re-walking Weather. The
`bindings` submit in options will re-assemble from `self._core` plus the
**existing** weather sub-dict (pulled from `_data["bindings"]`), and the
`weather` submit will re-assemble from the existing core plus new
weather. Both paths reconstruct the full `HABindings` dict from `_data`,
so either can be edited independently. (Exact assembly refactor to be
pinned down in the implementation plan; the invariant is: after any
sub-step, `_data["bindings"]` is a complete, valid `HABindings` dict.)

### 3. Auto-connect at init

The `connect` step's real work is a side effect: it polls Rachio to fill
`_devices`/`_api_key`, which the Core device dropdown and the live zone
picker depend on. Since the hub lets the admin skip Connect, run
`_connect_rachio(self._existing_bindings().get("rachio_api_key_secret",
DEFAULT_RACHIO_API_KEY_SECRET))` at init so pickers are live without
visiting Connect.

Cost: one Rachio poll per options-open — same as today, where `connect`
already runs first. No regression. Failure degrades exactly as today
(empty devices → free-text / manual fallbacks).

Connect stays a distinct menu item (long help text + a "re-poll after I
rotated my key" affordance).

### 4. Persist-on-mutation with a single guarded reload (approach A)

**Persist:** an options-only helper `_persist()` calls
`self.hass.config_entries.async_update_entry(self.config_entry,
data=self._data)`. Call it at the end of every options sub-step before
returning to the menu (connect, bindings, weather, add/edit/remove zone,
advanced). After each, the entry on disk is complete and current.

**Suppress reload during the flow:** `_reload_on_options` checks a flag
and no-ops while an options flow is active:

```python
async def _reload_on_options(hass, entry):
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if store.get("suppress_reload"):
        return
    await hass.config_entries.async_reload(entry.entry_id)
```

- Set `store["suppress_reload"] = True` at options `async_step_init`.
- At `finish` (`_async_finish`): clear the flag, then fire exactly one
  `await hass.config_entries.async_reload(entry.entry_id)`, then
  `async_create_entry(title="", data={})`.

The `async_update_entry` calls during the flow still fire the listener,
but it returns early — no reload, no `config.yaml` regeneration, no
scheduler restart until "Done."

**Abandonment caveat:** HA has no clean "options dialog abandoned" hook.
If the admin closes the dialog without "Done":

- Their committed edits are **already persisted** to the entry (nothing
  lost — the chip's goal is met).
- `suppress_reload` stays `True`, so `config.yaml` is **not regenerated**
  until the next reload. The next `async_setup_entry` overwrites
  `hass.data[DOMAIN][entry_id]` wholesale (see `__init__.py`, the
  `hass.data.setdefault(...)[entry.entry_id] = {...}` assignment),
  clearing the flag, so any later manual reload / HA restart /
  "Done"-through-options fully applies the saved data.

This is an accepted trade: durability of edits over immediate
regeneration on abandon. Documented in code comments.

### 5. First-install (config) flow — unchanged

`GeodropsRachioConfigFlow` keeps `_is_options = False` and the linear
chain `connect → bindings → weather → zone (→ add_another) → advanced →
async_create_entry`. No seeding, no `_persist()`, no reload guard (the
entry doesn't exist until `_async_finish`). The `_is_options` branches
already in `async_step_zone_details` / `_async_step_zone_manual` /
`async_step_weather` keep the two flows' "next step" wiring separate.

## Data flow

```
Options open
  └ async_step_init
      ├ seed _data from existing (bindings, zones, calib, overrides)
      ├ store.suppress_reload = True
      ├ _connect_rachio(existing secret)   # live pickers
      └ async_step_menu ─────────────► HUB
                                         │
   ┌──── connect/bindings/weather ◄──────┤  each: edit → _persist() → HUB
   │     add/edit/remove zone     ◄──────┤  (async_update_entry, no reload)
   │     advanced                 ◄──────┤
   └─────────────────────────────────────┘
                                         │
                    finish ──────────────┘
                      ├ store.suppress_reload = False
                      ├ async_reload(entry)        # single scheduler restart
                      └ async_create_entry("", {})
```

## Error handling

- Rachio poll failure at init: unchanged degrade path (empty devices →
  free-text device / manual zones). Menu still works.
- `invalid_notify_service` / `duplicate_zone_key`: unchanged; re-render
  the sub-step form with errors, no persist until the step validates.
- `advanced_overrides` invalid YAML: today it surfaces as
  `ConfigEntryNotReady` on reload. With guarded reload, this now surfaces
  at "Done" (the single reload) rather than mid-flow. The `advanced`
  sub-step should validate the YAML **before** `_persist()` so a bad
  override can't be persisted; pin the exact validation call in the plan.

## Testing

Extend `tests/test_config_flow.py` and `tests/test_init.py`:

- **Navigation:** options flow first screen is the hub menu (not
  `connect`). Each menu item opens its step and returns to the menu.
- **Seed:** open options, go straight to `finish` without editing →
  saved entry equals the original (bindings, zones, advanced all intact).
- **Persist-on-mutation:** add a zone, assert `async_update_entry` was
  called and `entry.data["zones"]` contains it *before* `finish`.
- **Guarded reload:** during the flow, `async_update_entry` calls do
  **not** trigger `async_reload` (assert reload count 0 while
  `suppress_reload` is set); `finish` triggers exactly **one** reload.
- **Abandonment:** persist a zone, do not call `finish` → entry data
  updated, reload count 0.
- **Edit independence:** edit Core only → `_data["bindings"]` still holds
  the original weather sub-dict (and vice versa).
- **Config flow unchanged:** first-install still walks
  connect→bindings→weather→zone→advanced and creates the entry; no
  `async_update_entry`/guard involved.

## Files touched

| File | Change |
|------|--------|
| `config_flow.py` | Rename `manage_zones`→`menu`; options `init` seeds + connects + shows menu; add `advanced`/`connect`/`bindings`/`weather` to menu; each options sub-step `_persist()`s and returns to menu; `finish`→save; add `_persist()` helper. |
| `__init__.py` | `_reload_on_options` honors `suppress_reload`. |
| `strings.json`, `translations/en.json` | Retitle hub; add menu items; keep step strings. |
| `tests/test_config_flow.py`, `tests/test_init.py` | Cases above. |
