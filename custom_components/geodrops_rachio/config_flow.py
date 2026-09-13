from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class GeodropsRachioConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
