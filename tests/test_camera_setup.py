"""Add live-view entities to core devices without registering helper devices."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.simplirtc import PLATFORMS
from custom_components.simplirtc.camera import SimpliSafeCamera


@pytest.mark.asyncio
async def test_entity_platform_links_the_existing_device_without_registering_one():
    assert PLATFORMS == [Platform.CAMERA]
    parent = SimpleNamespace(id='native-camera', name='User camera name', model='Native model')
    camera = SimpleNamespace(serial='camera-uuid', camera_settings={})
    # Isolate core's API/coordinator setup; exercise the real SimpliRTC constructor
    # and HA's entity-platform attachment path.
    def init_native(self, *args, **kwargs):
        self._attr_unique_id = camera.serial
        self._attr_device_info = DeviceInfo(identifiers={('simplisafe', camera.serial)}, name='Inherited name')
    with patch('custom_components.simplirtc.camera.SimpliSafeEntity.__init__', init_native):
        entity = SimpliSafeCamera(None, None, camera, None, parent)
    assert entity.device_info is None
    assert entity.device_entry is parent
    assert entity.unique_id == 'camera-uuid-camera'
    with patch('homeassistant.helpers.entity_platform.dr.async_get') as get_registry:
        device = EntityPlatform._async_get_entity_device(SimpleNamespace(), entity, 'helper', None)
    assert device is parent
    get_registry.assert_not_called()
    assert parent.name == 'User camera name'
    assert parent.model == 'Native model'
