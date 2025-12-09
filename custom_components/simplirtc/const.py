from __future__ import annotations

from homeassistant.components.simplisafe import (
	DOMAIN as SIMPLISAFE_DOMAIN,
	SimpliSafe,
)
from homeassistant.util.hass_dict import (
	HassEntryKey,
)

DOMAIN = "simplirtc"

ENTRY_KEY: HassEntryKey[SimpliSafe] = HassEntryKey(SIMPLISAFE_DOMAIN)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
CONF_LIVEKIT_RTSP_PROXY = "livekit_rtsp_proxy"
