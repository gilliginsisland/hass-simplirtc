# SimpliRTC

SimpliRTC adds live camera viewing, snapshot images, and camera motion events
to Home Assistant's SimpliSafe integration.

## Setup

1. Set up the built-in SimpliSafe integration in Home Assistant.
2. Add this repository to HACS and install SimpliRTC, or copy
   `custom_components/simplirtc` into your Home Assistant `custom_components`
   folder.
3. Add the following to your `configuration.yaml`:

   ```yaml
   simplirtc:
   ```

4. Restart Home Assistant.

SimpliRTC automatically adds supported cameras from your SimpliSafe account.

## Camera features

- **Live viewing:** Watch supported SimpliSafe cameras in Home Assistant.
- **Snapshot images:** See the latest available image from the camera's event
  history. An event triggered by another camera or sensor can also provide a
  newer image for this camera. These images are event snapshots, so they may
  not show the camera's current view.
- **Motion events:** Use camera motion events in Home Assistant automations.
  SimpliSafe does not provide a matching motion-clear event, so motion is
  exposed as an event rather than an on/off sensor.

## Supported systems

Only SimpliSafe V3 systems are supported.

SimpliSafe uses different services for live video. SimpliRTC supports live
viewing through AWS Kinesis Video Streams or LiveKit. Cameras using other
services do not get a live-view entity, but can still appear as devices with
motion events.

Your account's video service is chosen by SimpliSafe and can change without a
camera or base-station firmware update. SimpliRTC's author currently has
LiveKit cameras and cannot directly test Kinesis cameras.

Contributions to support additional systems and cameras are welcome.
