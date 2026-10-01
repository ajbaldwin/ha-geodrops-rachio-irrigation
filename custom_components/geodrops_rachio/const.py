DOMAIN = "geodrops_rachio"
PLATFORMS = ["select", "button", "switch", "sensor", "binary_sensor"]
DROUGHT_LEVELS = [
    "Level 0 - Normal", "Level 1 - Mild", "Level 2 - Significant",
    "Level 3 - Critical", "Level 4 - Emergency",
]
DEFAULT_DROUGHT_LEVEL = "Level 1 - Mild"
# Settings kept in entry.options (since 1.3); everything else the wizard
# collects is setup data in entry.data. See config_writer.entry_config.
SETTINGS_KEYS = ("self_calibration_enabled", "advanced_overrides")
