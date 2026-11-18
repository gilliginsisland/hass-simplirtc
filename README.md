# SimpliRTC

SimpliRTC adds live camera viewing and snapshot images to the camera devices
provided by Home Assistant's built-in SimpliSafe integration.

## Setup

1. Set up the built-in SimpliSafe integration in Home Assistant.
2. Install SimpliRTC through Marketplace, or copy
   `custom_components/simplirtc` into your Home Assistant `custom_components`
   folder, then restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and select
   **Simplisafe WebRTC Cameras**.

Set up SimpliRTC once. It automatically adds live-view entities to supported
cameras across all your built-in SimpliSafe accounts, including accounts and
cameras you add later. Entities appear on the existing SimpliSafe devices.

Existing installations are upgraded automatically and keep their entity IDs.
If you previously added `simplirtc:` to `configuration.yaml`, you can remove it;
configuration is now managed in the UI.

## Camera features

- **Live viewing:** Watch supported SimpliSafe cameras in Home Assistant.
- **Snapshot images:** See the latest available image from the camera's event
  history. An event triggered by another camera or sensor can also provide a
  newer image for this camera. These images are event snapshots, so they may
  not show the camera's current view.

Camera motion and doorbell events are provided by Home Assistant's built-in
SimpliSafe integration. SimpliRTC does not add duplicate event entities.
If an existing automation uses a SimpliRTC motion event, select the built-in
SimpliSafe motion event entity instead.

## Supported systems

Only SimpliSafe V3 systems are supported.

SimpliSafe uses different services for live video. SimpliRTC supports live
viewing through AWS Kinesis Video Streams or LiveKit. Cameras using other
services do not get a SimpliRTC live-view entity. Their devices and motion
events remain available through the built-in SimpliSafe integration.

Your account's video service is chosen by SimpliSafe and can change without a
camera or base-station firmware update. SimpliRTC's author currently has
LiveKit cameras and cannot directly test Kinesis cameras.

Contributions to support additional systems and cameras are welcome.
