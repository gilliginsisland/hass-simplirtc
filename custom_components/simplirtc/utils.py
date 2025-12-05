from __future__ import annotations

import logging
import os
import platform
import re
import stat
import subprocess
import asyncio
from threading import Thread
from urllib.parse import urlparse

import requests
import voluptuous as vol
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BINARY_VERSION = "0.4.3"

ASSETS = {
    "Darwin": {
        "arm64": "livekit-rtsp-darwin-arm64",
        "x86_64": "livekit-rtsp-darwin-amd64",
    },
    "Linux": {
        "aarch64": "livekit-rtsp-linux-arm64",
        "x86_64": "livekit-rtsp-linux-amd64",
    },
}

DEFAULT_URL = "rtsp://127.0.0.1:8854"

BINARY_NAME = re.compile(
    r"^(livekit-rtsp-\d\.\d\.\d+)(\.exe)?$"
)


def ensure_binary(hass: HomeAssistant) -> str | None:
    filename = hass.config.path(f"livekit-rtsp-{BINARY_VERSION}")
    try:
        if os.path.isfile(filename) and subprocess.run([filename, "-h"], check=True):
            return filename
    except:
        pass

    # remove all old binaries
    for file in os.listdir(hass.config.config_dir):
        if BINARY_NAME.match(file):
            _LOGGER.debug(f"Remove old binary: {file}")
            os.remove(hass.config.path(file))

    asset = ASSETS.get(platform.system(), {}).get(platform.machine())
    if not asset:
        return None

    # download new binary
    url = f"https://github.com/gilliginsisland/livekit-ffmpeg/releases/download/v{BINARY_VERSION}/{asset}"
    _LOGGER.debug(f"Download new binary: {url}")
    r = requests.get(url)
    if not r.ok:
        return None

    raw = r.content

    # save binary to config folder
    with open(filename, "wb") as f:
        f.write(raw)

    # change binary access rights
    os.chmod(filename, os.stat(filename).st_mode | stat.S_IEXEC)

    return filename


async def check_rtsp_server(rtsp_url: str, timeout: int = 2) -> bool:
    """
    Checks if an RTSP server is reachable and responsive to an OPTIONS request.

    Args:
        rtsp_url: The full RTSP URL (e.g., "rtsp://user:pass@host:port/stream").
        timeout: Connection timeout in seconds.

    Returns:
        The URL string if successful (status 200 OK), None otherwise.
    """

    # Parse the RTSP URL to extract host, port, and path
    parsed_url = urlparse(rtsp_url)
    host = parsed_url.hostname
    port = parsed_url.port or 554

    if not host:
        _LOGGER.debug("Invalid RTSP URL {rtsp_url}: missing hostname.")
        return False

    try:
        # Use asyncio.open_connection to establish TCP connection
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )

        # Manually craft the RTSP OPTIONS request string
        writer.write((
            f"OPTIONS {rtsp_url} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"\r\n"
        ).encode('utf-8'))
        await writer.drain()

        # Read the first line of the response (status line)
        response_line = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout
        )

        # Close the connection
        writer.close()
        await writer.wait_closed()
    except:
        return False

    return "RTSP/1.0 200 OK" == response_line.decode('utf-8').strip()


def validate_rtsp_base_url(value):
    """
    Validates that a URL is an RTSP URL with no path, query, or fragment.
    """
    parsed_url = urlparse(str(value))
    if parsed_url.scheme != 'rtsp':
        raise vol.Invalid("URL must use 'rtsp://' scheme.")
    if not parsed_url.netloc:
        raise vol.Invalid("URL must contain a network location (hostname or IP, and optional port).")
    if parsed_url.path and parsed_url.path != '/': # Allow empty or root path
        raise vol.Invalid("URL must not contain a path.")
    if parsed_url.query:
        raise vol.Invalid("URL must not contain query parameters.")
    if parsed_url.fragment:
        raise vol.Invalid("URL must not contain URL fragments.")
    return str(value)


class Server(Thread):
    def __init__(self, binary: str):
        super().__init__(name=DOMAIN, daemon=True)
        self.binary = binary
        self.process = None

    @property
    def available(self):
        return self.process.poll() is None if self.process else False

    def run(self):
        while self.binary:
            self.process = subprocess.Popen(
                [self.binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            # check alive
            while self.process.poll() is None:
                line = self.process.stdout.readline()
                if line == b"":
                    break
                _LOGGER.debug(line[:-1].decode())

    def stop(self, *args):
        self.binary = None
        self.process.terminate()
