from __future__ import annotations

import logging
import os
import platform
import re
import stat
import subprocess
import asyncio
import time
from threading import Thread
from typing import Any
from urllib.parse import urlparse

import requests

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BINARY_VERSION = "0.4.4"

REPO = "gilliginsisland/livekit-ffmpeg"
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

DEFAULT_URL = "127.0.0.1:8854"

BINARY_NAME = re.compile(
    r"^(livekit-rtsp-\d\.\d\.\d+)(\.exe)?$"
)


def ensure_binary(hass: HomeAssistant) -> str | None:
    filename = hass.config.path(f"livekit-rtsp-{BINARY_VERSION}")
    try:
        if os.path.isfile(filename) and subprocess.run(
            [filename, "-h"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        ):
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
    url = f"https://github.com/{REPO}/releases/download/v{BINARY_VERSION}/{asset}"
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


class Server(Thread):
    def __init__(self, *args: str):
        super().__init__(name=DOMAIN, daemon=True)
        self.args = args
        self.process = None

    @property
    def available(self):
        return self.process.poll() is None if self.process else False

    def run(self):
        while self.args:
            self.process = subprocess.Popen(
                self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            # check alive
            while self.process.poll() is None:
                assert (stdout := self.process.stdout) is not None
                line = stdout.readline()
                if line == b"":
                    break
                _LOGGER.debug(line[:-1].decode())

            time.sleep(2)

    def stop(self, *_: Any):
        self.args = None
        if self.process is not None:
            self.process.terminate()
