"""Asyncio MPD client helpers: connect-with-backoff + capability probe.

Thin functional wrapper around ``mpd.asyncio.MPDClient``. The daemon
keeps a direct reference to the client and ``await``s commands on it;
this module only abstracts the connect/retry policy and the capability
mapping so the daemon code stays focused on state translation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from functools import partial

import mpd
from mpd.asyncio import CommandResult, MPDClient

logger = logging.getLogger(__name__)

CONNECT_BACKOFF_MIN = 1.0
CONNECT_BACKOFF_MAX = 30.0
CONNECT_BACKOFF_FACTOR = 1.5
CONNECT_TIMEOUT = 10.0
CONFIG_PROBE_TIMEOUT = 5.0


def is_unix_socket(host: str) -> bool:
    """``/path`` = filesystem socket, ``@name`` = Linux abstract socket."""
    return host.startswith(("/", "@"))


async def connect(
    host: str,
    port: int,
    password: str | None = None,
    *,
    retry: bool = True,
) -> MPDClient:
    """Open an MPD connection. With ``retry=True`` loop with exponential
    backoff until a connection succeeds (or the caller cancels us).
    Authentication failures are *not* retried — they bubble out.
    Each attempt is capped at ``CONNECT_TIMEOUT`` seconds so a
    silently-dropped TCP SYN doesn't hang the daemon at startup.
    """
    backoff = CONNECT_BACKOFF_MIN
    while True:
        client = MPDClient()
        connected = False
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await client.connect(host, port)
                if password:
                    await client.password(password)
            connected = True
            endpoint = host if is_unix_socket(host) else f"{host}:{port}"
            logger.info("connected to MPD at %s", endpoint)
            return client
        except mpd.CommandError as e:
            logger.error("MPD auth/command error during connect: %s", e)
            raise
        except (OSError, mpd.ConnectionError, TimeoutError) as e:
            if not retry:
                raise
            logger.warning("MPD connect failed (%s); retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * CONNECT_BACKOFF_FACTOR, CONNECT_BACKOFF_MAX)
        finally:
            if not connected:
                with contextlib.suppress(Exception):
                    client.disconnect()


async def fetch_config(client: MPDClient) -> dict[str, str]:
    """Send MPD's ``config`` command and parse its multi-pair response as a
    dict — python-mpd2 3.1.x mis-parses it (its handler reads only single-pair
    responses, dropping the data), so we reuse the lib's command queue with a
    dict-parsing callback. Socket-only (TCP gets "Access denied").
    """
    def _parse_as_dict(client_: MPDClient, lines: list) -> dict[str, str]:
        return dict(client_._parse_pairs(lines))

    result = CommandResult("config", (), partial(_parse_as_dict, client))
    try:
        # ``__command_queue`` is name-mangled inside the mpd.asyncio.MPDClient
        # class; access it via the mangled attribute name.
        await client._MPDClient__command_queue.put(result)
        client._end_idle()
        client._write_command("config")
    except AttributeError as e:
        logger.warning("python-mpd2 private API moved (%s); skipping config probe", e)
        return {}
    except (mpd.ConnectionError, OSError) as e:
        # Connection died between put() and write_command(): the
        # CommandResult is orphaned in the queue, but run_loop drops the
        # client right after this returns, so it gets GC'd with the rest.
        logger.debug("MPD lost during config probe: %s", e)
        return {}

    try:
        async with asyncio.timeout(CONFIG_PROBE_TIMEOUT):
            parsed: dict[str, str] = await result
    except (TimeoutError, mpd.ConnectionError, OSError) as e:
        logger.debug("config probe gave up: %s", e)
        return {}
    return parsed


def capabilities(commands: Iterable[str]) -> dict[str, bool]:
    """Map the result of ``await client.commands()`` to feature flags.
    Each MPD command in the table below was added in a specific server
    version; checking the per-command list rather than parsing the
    version string handles forks (mopidy etc.) gracefully too.
    """
    cmds = set(commands)
    return {
        "idle": "idle" in cmds,                # 0.14
        "single": "single" in cmds,            # 0.15
        "albumart": "albumart" in cmds,        # 0.21
        "readpicture": "readpicture" in cmds,  # 0.22
        # Queue editing (TrackList CanEditTracks) — read-only permission
        # profiles expose none of these.
        "queue_edit": {"addid", "deleteid", "playid"} <= cmds,
    }
