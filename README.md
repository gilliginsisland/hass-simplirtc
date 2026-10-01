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

## LiveKit cameras returning 404 on live-view

Since around September 2026, SimpliSafe answers the v2 `live-view` request with
`404 Not Found` for cameras on the LiveKit (`mist`) backend when the request uses
the token from Home Assistant's SimpliSafe integration. That token belongs to
SimpliSafe's iOS-app Auth0 client. The same request with a token from the
SimpliSafe **web app** client (`DWkIUe6LC38xLomvfG6LXesCCaKJGl24`) returns the
LiveKit details as before; the scopes and audience of the two tokens are
otherwise the same. The symptom is one snapshot frame, then no video (#3).

SimpliRTC can use a separate web-app token for the live-view request only. If
`/config/.storage/simplirtc_webapp_token.json` exists, it is used (and refreshed,
with the rotated refresh token written back); otherwise nothing changes. The
SimpliSafe integration itself keeps its own token.

To create the file:

1. Log in at <https://webapp.simplisafe.com> in a desktop browser.
2. In the same browser, open
   <https://auth.simplisafe.com/.well-known/openid-configuration>, open the
   developer console on that tab, and run:

```js
// SimpliRTC: create the web-app token file. Log in at https://webapp.simplisafe.com first, then open
// https://auth.simplisafe.com/.well-known/openid-configuration and run this in that tab's DevTools console.
(async () => {
	const CLIENT = 'DWkIUe6LC38xLomvfG6LXesCCaKJGl24', REDIRECT = 'https://webapp.simplisafe.com/';
	const b64u = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
	const verifier = b64u(crypto.getRandomValues(new Uint8Array(48)));
	const challenge = b64u(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
	const params = new URLSearchParams({client_id: CLIENT, redirect_uri: REDIRECT, audience: 'https://api.simplisafe.com/',
		scope: 'openid profile email offline_access https://api.simplisafe.com/scopes/user:platform', response_type: 'code',
		response_mode: 'form_post', state: crypto.randomUUID(), nonce: crypto.randomUUID(), code_challenge: challenge,
		code_challenge_method: 'S256', device: 'Home Assistant SimpliRTC', device_id: crypto.randomUUID().toUpperCase()});
	let r = await fetch('/authorize?' + params, {credentials: 'include'}), html = await r.text();
	for (let i = 0; i < 4 && !/name="code"/.test(html); i++) {  // step through the MFA browser-capability form
		const f = new DOMParser().parseFromString(html, 'text/html').querySelector('form');
		if (!f) break;
		const body = new URLSearchParams();
		f.querySelectorAll('input').forEach(x => x.name && body.append(x.name, x.value));
		for (const [k, v] of [['js-available', 'true'], ['webauthn-available', 'false'], ['is-brave', 'false'], ['webauthn-platform-available', 'false']])
			if (body.has(k)) body.set(k, v);
		r = await fetch(new URL(f.getAttribute('action') || r.url, r.url), {method: 'POST', credentials: 'include', body});
		html = await r.text();
	}
	const code = (html.match(/name="code"\s+value="([^"]+)"/) || [])[1];
	if (!code) throw new Error('No authorization code; are you logged in at webapp.simplisafe.com?');
	const tok = await (await fetch('/oauth/token', {method: 'POST', headers: {'Content-Type': 'application/json'},
		body: JSON.stringify({grant_type: 'authorization_code', client_id: CLIENT, code, code_verifier: verifier, redirect_uri: REDIRECT})})).json();
	if (!tok.refresh_token) throw new Error('Token exchange failed: ' + JSON.stringify(tok));
	const file = {client_id: CLIENT, refresh_token: tok.refresh_token, access_token: tok.access_token, expires_at: Date.now() / 1000 + tok.expires_in - 60};
	console.log('Save this as /config/.storage/simplirtc_webapp_token.json:\n' + JSON.stringify(file));
})();
```

3. Save the printed JSON as `/config/.storage/simplirtc_webapp_token.json`
   (keep it private, e.g. `chmod 600`), then restart Home Assistant.

The snippet runs SimpliSafe's normal web-app login flow (authorization code with
PKCE) using your existing browser session; the token goes only into that file.

## Advanced: Backend Names

The backend value comes from the camera admin settings as `webRTCProvider`.
SimpliRTC currently recognizes these values:

- `kvs`: AWS Kinesis Video Streams
- `mist`: LiveKit

Any other `webRTCProvider` value is treated as unknown and skipped.
