# SimpliRTC

SimpliRTC adds native WebRTC support for SimpliSafe cameras in Home Assistant.

SimpliRTC requires the native Home Assistant SimpliSafe integration to be set up,
and adds camera support to the existing integration.

## Setup

Install SimpliRTC either by adding this repository to HACS and installing the
SimpliRTC integration, or by copying `custom_components/simplirtc` into your
Home Assistant `custom_components` folder.

Add SimpliRTC to your `configuration.yaml`:

```yaml
simplirtc:
```

Add and configure the normal Home Assistant SimpliSafe integration.

Once the SimpliSafe integration is loaded, SimpliRTC watches for its config
entries and automatically adds camera support for supported cameras.

SimpliRTC also adds motion event entities for V3 cameras. These entities are
backed by SimpliSafe websocket motion events, so they do not depend on the
camera's WebRTC backend. SimpliSafe does not appear to expose a camera
motion-active API state or a matching motion-clear event, so motion is exposed
as an event rather than a latched on/off state.

## Supported Systems

Only SimpliSafe V3 systems are supported. Older system versions are skipped.

SimpliSafe uses different video backends for different accounts and systems.
Some cameras use AWS Kinesis Video Streams, and some use LiveKit. SimpliRTC
supports both of those backends.

Some reported systems use a different backend that SimpliRTC does not currently
support. Cameras on unknown backends are intentionally ignored instead of being
added as broken camera entities.

Pull requests are welcome for adding support for other systems or video
backends. I try to keep Kinesis support working, but I can no longer test it
directly because my system now uses LiveKit.

I do not know what causes SimpliSafe to choose a specific backend. It does not
appear to be only camera or base-station firmware. For example, my system was
originally on Kinesis and was later switched to LiveKit without changing the
camera or base-station firmware.

## Advanced: Backend Names

The backend value comes from the camera admin settings as `webRTCProvider`.
SimpliRTC currently recognizes these values:

- `kvs`: AWS Kinesis Video Streams
- `mist`: LiveKit

Any other `webRTCProvider` value is treated as unknown and skipped.
