from homeassistant.components.simplisafe import (
	DOMAIN as SIMPLISAFE_DOMAIN,
	SimpliSafe,
)
from homeassistant.util.hass_dict import HassEntryKey

DOMAIN = "simplirtc"

ENTRY_KEY: HassEntryKey[SimpliSafe] = HassEntryKey(SIMPLISAFE_DOMAIN)
