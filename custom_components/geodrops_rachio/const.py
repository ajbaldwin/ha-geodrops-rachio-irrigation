DOMAIN = "geodrops_rachio"
PLATFORMS = [
    "select", "number", "button", "switch", "sensor", "binary_sensor"]
DROUGHT_LEVELS = [
    "Level 0 - Normal", "Level 1 - Mild", "Level 2 - Significant",
    "Level 3 - Critical", "Level 4 - Emergency",
]
DEFAULT_DROUGHT_LEVEL = "Level 1 - Mild"
# Settings kept in entry.options (since 1.3); everything else the wizard
# collects is setup data in entry.data. See config_writer.entry_config.
SETTINGS_KEYS = ("self_calibration_enabled", "advanced_overrides")

# The drought level select's default entity id.
DROUGHT_LEVEL_ENTITY = "select.geodrops_rachio_drought_level"


def level_changed_signal(entry_id: str) -> str:
    """Dispatcher signal the drought level select sends when the user picks a
    different level."""
    return f"geodrops_rachio_{entry_id}_drought_level_changed"
