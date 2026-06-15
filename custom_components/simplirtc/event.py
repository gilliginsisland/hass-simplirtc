"""Event support for SimpliSafe cameras."""

from __future__ import annotations

from typing import cast, override

from simplipy.device.camera import Camera
from simplipy.system.v3 import SystemV3
from simplipy.websocket import EVENT_CAMERA_MOTION_DETECTED, WebsocketEvent

from homeassistant.components.event import (
	EventDeviceClass,
	EventEntity,
	EventEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

EVENT_TYPE_MOTION = "motion"
CAMERA_EVENT_SERIAL_KEYS = ("uuid", "serial")


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry[SimpliSafe],
	async_add_entities: AddEntitiesCallback,
) -> None:
	"""Set up SimpliSafe camera events."""
	simplisafe = entry.runtime_data

	events: list[SimpliSafeCameraMotionEvent] = []

	for system in simplisafe.systems.values():
		if not isinstance(system, SystemV3):
			continue

		events.extend(
			SimpliSafeCameraMotionEvent(simplisafe, system, camera)
			for camera in system.cameras.values()
		)

	async_add_entities(events)


class SimpliSafeCameraMotionEvent(  # pyright: ignore[reportIncompatibleVariableOverride]
	SimpliSafeEntity, EventEntity
):
	"""Event entity for camera motion events."""

	_attr_name = "Motion event"

	def __init__(
		self,
		simplisafe: SimpliSafe,
		system: SystemV3,
		device: Camera,
	) -> None:
		"""Initialize the SimpliSafe camera motion event."""
		super().__init__(
			simplisafe,
			system,
			device=device,
			additional_websocket_events=(EVENT_CAMERA_MOTION_DETECTED,),
		)
		self.entity_description = EventEntityDescription(
			key="motion",
			device_class=EventDeviceClass.MOTION,
			event_types=[EVENT_TYPE_MOTION],
		)
		self._attr_unique_id = f"{super().unique_id}-motion-event"
		self._device: Camera
		self._event_serials = self._camera_event_serials()

	@override
	@callback
	def async_update_from_websocket_event(self, event: WebsocketEvent) -> None:
		"""Update the entity when camera motion is reported."""
		self._trigger_event(
			EVENT_TYPE_MOTION,
			{
				"event_info": event.info,
				"event_timestamp": event.timestamp.isoformat(),
			},
		)
		self.async_reset_error_count()

	@override
	@callback
	def _handle_websocket_update(self, event: WebsocketEvent) -> None:
		"""Update the entity with new websocket data."""
		if (
			event.event_type == EVENT_CAMERA_MOTION_DETECTED
			and event.sensor_serial not in self._event_serials
		):
			return

		super()._handle_websocket_update(event)

	def _camera_event_serials(self) -> set[str]:
		"""Return camera identifiers that may appear in websocket events."""
		serials = {self._device.serial}
		system = cast(SystemV3, self._system)
		camera_data = system.camera_data.get(self._device.serial, {})
		for key in CAMERA_EVENT_SERIAL_KEYS:
			if isinstance(serial := camera_data.get(key), str) and serial:
				serials.add(serial)
		return serials
