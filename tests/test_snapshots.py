"""Select this camera's snapshot regardless of the event's triggering device."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.simplirtc.camera import SimpliSafeCamera


def video(url):
    return {"_links": {"snapshot/jpg": {"href": url}}}


def camera_with_history(events):
    api = SimpleNamespace(
        async_request=AsyncMock(return_value={"events": events}),
        async_media=AsyncMock(return_value=b"jpeg"),
    )
    camera = SimpleNamespace(
        _device=SimpleNamespace(serial="camera-serial"),
        _system=SimpleNamespace(system_id=1, camera_data={
            "camera-serial": {"uuid": "camera-uuid", "serial": "camera-serial"},
        }),
        _simplisafe=SimpleNamespace(_api=api),
        _snapshot_template_url=None,
    )
    camera._camera_event_serials = lambda: SimpliSafeCamera._camera_event_serials(camera)
    camera._async_refresh_snapshot_template_url = lambda: SimpliSafeCamera._async_refresh_snapshot_template_url(camera)
    return camera, api


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_id", ["camera-uuid", "camera-serial"])
async def test_snapshot_from_other_camera_event_is_selected_and_cached(camera_id):
    camera, api = camera_with_history([
        {"eventTimestamp": 200, "sensorSerial": "other-camera", "videoStartedBy": "other-uuid",
         "video": {"other-uuid": video("https://example.test/wrong-camera"),
                   camera_id: video("https://example.test/newest?size=full{&width}")}},
        # Unsorted input must not replace the newer cross-camera snapshot.
        {"eventTimestamp": 100, "sensorSerial": "camera-serial", "videoStartedBy": camera_id,
         "video": {camera_id: video("https://example.test/older")}},
        # A newer event without a usable snapshot must not mask the chosen image.
        {"eventTimestamp": 300, "video": {camera_id: {"_links": {}}}},
        {"eventTimestamp": 400, "video": {"other-uuid": {}}},
        {"eventTimestamp": 500},
    ])

    assert await SimpliSafeCamera.async_camera_image(camera, width=640) == b"jpeg"
    api.async_media.assert_awaited_with("https://example.test/newest?size=full&width=640")
    assert await SimpliSafeCamera.async_camera_image(camera) == b"jpeg"
    api.async_media.assert_awaited_with("https://example.test/newest?size=full")
    api.async_request.assert_awaited_once_with("get", "subscriptions/1/events?numEvents=50")


@pytest.mark.asyncio
async def test_snapshot_does_not_require_trigger_metadata():
    camera, _ = camera_with_history([
        {"eventTimestamp": 20, "video": {"camera-uuid": video("https://example.test/target")}},
    ])
    await camera._async_refresh_snapshot_template_url()
    assert camera._snapshot_template_url == "https://example.test/target"


@pytest.mark.asyncio
async def test_other_camera_snapshot_is_never_used_as_fallback():
    camera, api = camera_with_history([
        {"eventTimestamp": 30, "sensorSerial": "camera-serial", "videoStartedBy": "other-uuid",
         "video": {"other-uuid": video("https://example.test/other")}},
    ])
    assert await SimpliSafeCamera.async_camera_image(camera) is None
    api.async_media.assert_not_awaited()
