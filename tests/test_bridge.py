"""Unit tests for bridge.py pure helpers + the two-phase metadata/cover
emission — no MPD, no D-Bus.

These run on a partially-initialised ``MpdMprisBridge`` built via
``__new__`` (we skip the heavy ``__init__`` which needs a running event
loop). Only the attributes the methods under test read are set on it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import mpd
import pytest
from dbus_fast import Variant

from mpd2mpris.bridge import (
    MpdMprisBridge,
    _diff_queue,
    _is_external_seek,
    _RefreshSnapshot,
)
from mpd2mpris.translate import NO_TRACK


def _cover_bridge(cover_finder, *, client=None):
    """Minimal bridge stub for the background cover path (``_resolve_cover``).
    ``_last_base`` is set per-test."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.client = client or MagicMock()
    bridge.music_dir = Path("/srv/music")
    bridge.url_handlers = ["http://"]
    bridge.cover_finder = cover_finder
    bridge.player = MagicMock()
    bridge._art = None
    return bridge


# --- _is_external_seek -----------------------------------------------------

def test_seek_within_tolerance_is_not_external() -> None:
    # 10s ago elapsed=5.0, now=15s wall-clock, observed=15.0 → expected=15.0
    assert not _is_external_seek({"elapsed": "5.0"}, 0.0, 15.0, 10.0)


def test_seek_deviation_above_threshold_is_external() -> None:
    # 10s elapsed, but actual position jumped to 30s → external seek
    assert _is_external_seek({"elapsed": "5.0"}, 0.0, 30.0, 10.0)


def test_seek_deviation_at_threshold_is_not_external() -> None:
    # Exactly 0.6s deviation is the boundary; spec says > 0.6 only.
    assert not _is_external_seek({"elapsed": "5.0"}, 0.0, 15.6, 10.0)


def test_seek_deviation_just_above_threshold_is_external() -> None:
    assert _is_external_seek({"elapsed": "5.0"}, 0.0, 15.7, 10.0)


# --- _resolve_cover (background cover lookup) -----------------------------

@pytest.mark.asyncio
async def test_resolve_cover_no_song_url_skips_find() -> None:
    """A song with no resolvable URL must not call cover_finder.find."""
    cover_finder = MagicMock()
    cover_finder.find = AsyncMock(side_effect=AssertionError("should not be called"))
    bridge = _cover_bridge(cover_finder)
    base = {"xesam:title": Variant("s", "x")}
    bridge._last_base = base
    await bridge._resolve_cover({"title": "x"}, {}, base)
    cover_finder.find.assert_not_called()
    bridge.player.update_metadata.assert_not_called()  # no cover to add


@pytest.mark.asyncio
async def test_resolve_cover_attaches_arturl() -> None:
    cover_finder = MagicMock()
    cover_finder.find = AsyncMock(return_value="file:///cache/cover.jpg")
    bridge = _cover_bridge(cover_finder)
    base = {"xesam:title": Variant("s", "x")}
    bridge._last_base = base
    await bridge._resolve_cover(
        {"title": "x", "file": "Artist/Song.flac"}, {}, base,
    )
    emitted = bridge.player.update_metadata.call_args.args[0]
    assert emitted["mpris:artUrl"].value == "file:///cache/cover.jpg"
    assert emitted["xesam:title"].value == "x"  # base preserved
    assert bridge._art == "file:///cache/cover.jpg"  # recorded for re-emits


@pytest.mark.asyncio
async def test_resolve_cover_exception_swallowed(caplog) -> None:
    cover_finder = MagicMock()
    cover_finder.find = AsyncMock(side_effect=RuntimeError("cover lookup broke"))
    bridge = _cover_bridge(cover_finder)
    base = {"xesam:title": Variant("s", "x")}
    bridge._last_base = base
    with caplog.at_level("ERROR"):
        await bridge._resolve_cover(
            {"title": "x", "file": "Artist/Song.flac"}, {}, base,
        )
    bridge.player.update_metadata.assert_not_called()  # no cover to add
    assert any("cover lookup failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolve_cover_bails_when_track_changed() -> None:
    """A cover that resolves after the track moved on must not be emitted."""
    cover_finder = MagicMock()
    cover_finder.find = AsyncMock(return_value="file:///cache/cover.jpg")
    bridge = _cover_bridge(cover_finder)
    base = {"xesam:title": Variant("s", "x")}
    bridge._last_base = {"xesam:title": Variant("s", "newer")}  # changed meanwhile
    await bridge._resolve_cover(
        {"title": "x", "file": "Artist/Song.flac"}, {}, base,
    )
    bridge.player.update_metadata.assert_not_called()


# --- _previous_cdaware -----------------------------------------------------

def _mpd_client_with_status(elapsed: float, songid: str = "7"):
    client = MagicMock()
    client.status = AsyncMock(return_value={"elapsed": str(elapsed), "songid": songid})
    client.previous = AsyncMock()
    client.seekid = AsyncMock()
    return client


def _bridge_with_cdprev(cdprev: bool) -> MpdMprisBridge:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._cdprev = cdprev
    return bridge


@pytest.mark.asyncio
async def test_previous_cdaware_disabled_always_previous() -> None:
    bridge = _bridge_with_cdprev(False)
    client = _mpd_client_with_status(elapsed=12.0)
    await bridge._previous_cdaware(client)
    client.previous.assert_awaited_once()
    client.seekid.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_under_3s_skips_back() -> None:
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=1.5)
    await bridge._previous_cdaware(client)
    client.previous.assert_awaited_once()
    client.seekid.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_past_3s_seeks_to_start() -> None:
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=12.0, songid="42")
    await bridge._previous_cdaware(client)
    client.seekid.assert_awaited_once_with(42, 0)
    client.previous.assert_not_awaited()


@pytest.mark.asyncio
async def test_previous_cdaware_at_3s_seeks_to_start() -> None:
    # Boundary: the original used ``>= 3``.
    bridge = _bridge_with_cdprev(True)
    client = _mpd_client_with_status(elapsed=3.0, songid="9")
    await bridge._previous_cdaware(client)
    client.seekid.assert_awaited_once_with(9, 0)
    client.previous.assert_not_awaited()


# --- _snapshot -------------------------------------------------------------

def _snapshot_bridge(
    *,
    last_status: dict | None = None,
    last_song: dict | None = None,
    last_time: float = 0.0,
    now: float = 100.0,
) -> MpdMprisBridge:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._loop = MagicMock()
    bridge._loop.time = MagicMock(return_value=now)
    bridge.last_status = last_status if last_status is not None else {}
    bridge.last_song = last_song if last_song is not None else {}
    bridge.last_time = last_time
    return bridge


def test_snapshot_captures_old_and_advances_last() -> None:
    bridge = _snapshot_bridge(
        last_status={"state": "play"},
        last_song={"id": "1"},
        last_time=42.0,
        now=100.0,
    )
    new_status = {"state": "pause", "elapsed": "12.5"}
    new_song = {"id": "2"}

    snap = bridge._snapshot(new_status, new_song)

    assert snap.old_status == {"state": "play"}
    assert snap.old_song == {"id": "1"}
    assert snap.old_time == 42.0
    assert snap.now == 100.0
    assert snap.state == "pause"
    assert snap.new_pos_s == 12.5
    assert snap.same_song is False
    # self.last_* advanced to the new values.
    assert bridge.last_status is new_status
    assert bridge.last_song is new_song
    assert bridge.last_time == 100.0


def test_snapshot_same_song_when_ids_match() -> None:
    bridge = _snapshot_bridge(last_song={"id": "7"})
    snap = bridge._snapshot({"state": "play"}, {"id": "7"})
    assert snap.same_song is True


def test_snapshot_first_refresh_is_not_same_song() -> None:
    # No previous song → same_song must be False (cover resolved fresh
    # on the very first track).
    bridge = _snapshot_bridge()
    snap = bridge._snapshot({"state": "play"}, {"id": "1"})
    assert snap.same_song is False


def test_snapshot_state_defaults_to_stop_when_missing() -> None:
    bridge = _snapshot_bridge()
    snap = bridge._snapshot({}, {})
    assert snap.state == "stop"
    assert snap.new_pos_s == 0.0


# --- _apply_current_state --------------------------------------------------

def _apply_bridge() -> MpdMprisBridge:
    """Bridge with a mocked player (capture update_* calls); the background
    cover scheduler is mocked out so ``_apply_current_state`` only emits
    the cover-free base synchronously."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.client = MagicMock()
    bridge.music_dir = Path("/srv/music")
    bridge.url_handlers = ["http://"]
    bridge.player = MagicMock()
    bridge._last_base = {}
    bridge._art = None
    bridge._cover_task = None
    bridge._schedule_cover = MagicMock()  # type: ignore[method-assign]
    return bridge


def _snap(
    *,
    old_state: str = "stop", state: str = "play",
    old_time: float = 0.0, now: float = 10.0,
    old_elapsed: float = 0.0, new_pos_s: float = 0.0,
    same_song: bool = False, old_song: dict | None = None,
) -> _RefreshSnapshot:
    return _RefreshSnapshot(
        old_status={"state": old_state, "elapsed": str(old_elapsed)},
        old_song=old_song if old_song is not None else {},
        old_time=old_time,
        now=now,
        state=state,
        new_pos_s=new_pos_s,
        same_song=same_song,
    )


def test_apply_pushes_basic_player_state() -> None:
    bridge = _apply_bridge()
    status = {
        "state": "play", "elapsed": "5.0",
        "repeat": "1", "single": "1", "random": "1", "volume": "50",
    }
    bridge._apply_current_state(
        status, {"id": "1", "title": "x"},
        _snap(state="play", new_pos_s=5.0),
    )
    bridge.player.update_playback_status.assert_called_with("Playing")
    bridge.player.update_loop_status.assert_called_with("Track")
    bridge.player.update_shuffle.assert_called_with(True)
    bridge.player.update_volume.assert_called_with(0.5)
    bridge.player.update_position.assert_called_with(5_000_000)


def test_apply_skips_volume_when_unreportable() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play", "volume": "-1"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_volume.assert_not_called()


def test_apply_emits_seeked_on_external_seek() -> None:
    bridge = _apply_bridge()
    # 10s wall-clock elapsed since old_time=0, old elapsed=5 → expected 15s;
    # new_pos_s=30s → external seek.
    bridge._apply_current_state(
        {"state": "play"}, {"id": "1"},
        _snap(old_state="play", state="play", same_song=True,
              old_elapsed=5.0, old_time=0.0, now=10.0, new_pos_s=30.0),
    )
    bridge.player.emit_seeked.assert_called_once_with(30_000_000)


def test_apply_no_seeked_on_natural_progression() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play"}, {"id": "1"},
        _snap(old_state="play", state="play", same_song=True,
              old_elapsed=5.0, old_time=0.0, now=10.0, new_pos_s=15.0),
    )
    bridge.player.emit_seeked.assert_not_called()


def test_apply_no_seeked_on_song_change() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play"}, {"id": "2"},
        _snap(old_state="play", state="play", same_song=False,
              new_pos_s=30.0),
    )
    bridge.player.emit_seeked.assert_not_called()


def test_apply_can_go_next_from_nextsongid() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play", "nextsongid": "5"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_capabilities.assert_any_call(can_go_next=True)


def test_apply_can_go_next_from_repeat() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play", "repeat": "1"}, {"id": "1"}, _snap(),
    )
    bridge.player.update_capabilities.assert_any_call(can_go_next=True)


def test_apply_no_song_clears_metadata() -> None:
    bridge = _apply_bridge()
    bridge._last_base = {"xesam:title": Variant("s", "old")}
    bridge._art = "file:///cache/old.jpg"
    bridge._apply_current_state({"state": "stop"}, {}, _snap(state="stop"))
    bridge.player.update_metadata.assert_called_with({})
    bridge.player.update_capabilities.assert_any_call(can_seek=False)
    assert bridge._last_base == {}
    assert bridge._art is None


def test_apply_song_emits_cover_free_base_and_schedules_cover() -> None:
    bridge = _apply_bridge()
    bridge._apply_current_state(
        {"state": "play"},
        {"id": "1", "title": "Track", "time": "180"},
        _snap(state="play"),
    )
    emitted = bridge.player.update_metadata.call_args.args[0]
    assert "xesam:title" in emitted
    assert "mpris:artUrl" not in emitted  # cover resolves off the critical path
    bridge.player.update_capabilities.assert_any_call(can_seek=True)
    bridge._schedule_cover.assert_called_once()
    assert bridge._last_base == emitted


def test_apply_same_tags_skips_metadata_reemit() -> None:
    """A status-only refresh (identical tags) must not re-emit Metadata or
    restart the cover lookup — that would drop a resolved mpris:artUrl."""
    bridge = _apply_bridge()
    song = {"id": "1", "title": "Track", "time": "180"}
    bridge._apply_current_state({"state": "play"}, song, _snap(state="play"))
    bridge.player.update_metadata.reset_mock()
    bridge._schedule_cover.reset_mock()
    bridge._apply_current_state(
        {"state": "play"}, song, _snap(state="play", same_song=True),
    )
    bridge.player.update_metadata.assert_not_called()
    bridge._schedule_cover.assert_not_called()


def test_apply_carries_art_across_same_stream_title_change() -> None:
    """Web radio: the ICY title changes under the same song id. The cover
    already shown must be carried into the new emit, not blanked — this is
    the regression that left mpris:artUrl empty on every title change."""
    bridge = _apply_bridge()
    bridge._last_base = {"xesam:title": Variant("s", "old title")}
    bridge._art = "https://station/favicon.ico"
    bridge._apply_current_state(
        {"state": "play"},
        {"id": "2", "title": "New - Title", "name": "Some Radio"},
        _snap(state="play", same_song=True),
    )
    emitted = bridge.player.update_metadata.call_args.args[0]
    assert emitted["mpris:artUrl"].value == "https://station/favicon.ico"
    assert bridge._art == "https://station/favicon.ico"  # kept


def test_apply_drops_art_on_real_track_change() -> None:
    """A genuine track change (different song id) drops the old cover — a
    fresh one is coming via the scheduled lookup."""
    bridge = _apply_bridge()
    bridge._last_base = {"xesam:title": Variant("s", "prev")}
    bridge._art = "file:///cache/prev.jpg"
    bridge._apply_current_state(
        {"state": "play"},
        {"id": "9", "title": "Next", "time": "100"},
        _snap(state="play", same_song=False),
    )
    emitted = bridge.player.update_metadata.call_args.args[0]
    assert "mpris:artUrl" not in emitted


# --- MPRIS callbacks + task plumbing --------------------------------------
# These run the real ``_fire``/``_schedule``/``_mpd_safe`` chain on the live
# test event loop; the MPD client is a MagicMock whose commands are AsyncMocks.


def _mpd_client(**status: str):
    """MagicMock MPD client whose awaited commands are AsyncMocks; ``status``
    becomes the dict ``c.status()`` resolves to."""
    c = MagicMock()
    for name in ("play", "pause", "stop", "next", "previous", "random",
                 "setvol", "seekcur", "repeat", "single", "seekid"):
        setattr(c, name, AsyncMock())
    c.status = AsyncMock(return_value=dict(status))
    return c


def _callback_bridge(client):
    """Partially-initialised bridge with a real loop + task set, for the sync
    MPRIS callbacks. ``client`` may be ``None`` to exercise the no-connection
    no-op paths."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._loop = asyncio.get_running_loop()
    bridge.bg_tasks = set()
    bridge.client = client
    bridge.caps = {}
    bridge._cdprev = False
    bridge.last_song = {}
    return bridge


async def _drain(bridge) -> None:
    """Await every MPD task the callbacks scheduled."""
    await asyncio.gather(*list(bridge.bg_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("call, method, args", [
    (lambda b: b.on_play(), "play", ()),
    (lambda b: b.on_pause(), "pause", (1,)),
    (lambda b: b.on_stop(), "stop", ()),
    (lambda b: b.on_next(), "next", ()),
    (lambda b: b.on_shuffle_set(True), "random", (1,)),
    (lambda b: b.on_shuffle_set(False), "random", (0,)),
    (lambda b: b.on_volume_set(0.5), "setvol", (50,)),
])
async def test_simple_callback_fires_command(call, method, args) -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    call(bridge)
    await _drain(bridge)
    getattr(client, method).assert_awaited_once_with(*args)


@pytest.mark.asyncio
async def test_on_volume_set_rounds_to_nearest_percent() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.on_volume_set(0.337)
    await _drain(bridge)
    client.setvol.assert_awaited_once_with(34)


@pytest.mark.asyncio
async def test_on_seek_positive_is_relative_with_plus() -> None:
    # A forward seek is sent as a signed string so MPD treats it as relative.
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.on_seek(2_000_000)
    await _drain(bridge)
    client.seekcur.assert_awaited_once_with("+2.0")


@pytest.mark.asyncio
async def test_on_seek_negative_keeps_sign() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.on_seek(-1_500_000)
    await _drain(bridge)
    client.seekcur.assert_awaited_once_with("-1.5")


@pytest.mark.asyncio
async def test_on_previous_skips_to_previous_track() -> None:
    # cdprev disabled: a plain previous, no mid-track seek-to-start.
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.on_previous()
    await _drain(bridge)
    client.previous.assert_awaited_once_with()
    client.seekid.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_play_pause_pauses_when_playing() -> None:
    client = _mpd_client(state="play")
    bridge = _callback_bridge(client)
    bridge.on_play_pause()
    await _drain(bridge)
    client.pause.assert_awaited_once_with(1)
    client.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_play_pause_plays_when_not_playing() -> None:
    client = _mpd_client(state="stop")
    bridge = _callback_bridge(client)
    bridge.on_play_pause()
    await _drain(bridge)
    client.play.assert_awaited_once_with()
    client.pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_play_pause_no_client_is_noop() -> None:
    bridge = _callback_bridge(None)
    bridge.on_play_pause()
    assert bridge.bg_tasks == set()


@pytest.mark.asyncio
async def test_on_get_position_returns_microseconds() -> None:
    client = _mpd_client(elapsed="12.5")
    bridge = _callback_bridge(client)
    assert await bridge.on_get_position() == 12_500_000


@pytest.mark.asyncio
async def test_on_get_position_no_client_returns_none() -> None:
    bridge = _callback_bridge(None)
    assert await bridge.on_get_position() is None


@pytest.mark.asyncio
async def test_on_get_position_empty_status_returns_none() -> None:
    # No status (e.g. command error swallowed) → None so the interface keeps
    # its last cached Position.
    client = _mpd_client()  # status() resolves to {}
    bridge = _callback_bridge(client)
    assert await bridge.on_get_position() is None


@pytest.mark.asyncio
async def test_on_set_position_seeks_when_trackid_matches() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.last_song = {"id": "7"}
    bridge.on_set_position("/org/mpris/MediaPlayer2/Track/7", 3_000_000)
    await _drain(bridge)
    client.seekcur.assert_awaited_once_with("3.0")


@pytest.mark.asyncio
async def test_on_set_position_noop_on_trackid_mismatch() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.last_song = {"id": "7"}
    bridge.on_set_position("/org/mpris/MediaPlayer2/Track/9", 3_000_000)
    await _drain(bridge)
    client.seekcur.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_set_position_seeks_when_no_current_id() -> None:
    # With no known current id the spec's match check can't fail, so the seek
    # proceeds regardless of the supplied trackid.
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.last_song = {}
    bridge.on_set_position("/org/mpris/MediaPlayer2/Track/3", 1_000_000)
    await _drain(bridge)
    client.seekcur.assert_awaited_once_with("1.0")


@pytest.mark.asyncio
async def test_loop_playlist_sets_repeat_and_clears_single() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.caps = {"single": True}
    bridge.on_loop_status_set("Playlist")
    await _drain(bridge)
    client.repeat.assert_awaited_once_with(1)
    client.single.assert_awaited_once_with(0)


@pytest.mark.asyncio
async def test_loop_track_sets_repeat_and_single() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.caps = {"single": True}
    bridge.on_loop_status_set("Track")
    await _drain(bridge)
    client.repeat.assert_awaited_once_with(1)
    client.single.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_loop_none_clears_repeat_and_single() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.caps = {"single": True}
    bridge.on_loop_status_set("None")
    await _drain(bridge)
    client.repeat.assert_awaited_once_with(0)
    client.single.assert_awaited_once_with(0)


@pytest.mark.asyncio
async def test_loop_without_single_capability_skips_single() -> None:
    client = _mpd_client()
    bridge = _callback_bridge(client)
    bridge.caps = {}  # MPD too old for `single`
    bridge.on_loop_status_set("Track")
    await _drain(bridge)
    client.repeat.assert_awaited_once_with(1)
    client.single.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_no_client_is_noop() -> None:
    bridge = _callback_bridge(None)
    bridge.on_loop_status_set("Track")
    assert bridge.bg_tasks == set()


@pytest.mark.asyncio
async def test_fire_without_client_schedules_nothing() -> None:
    bridge = _callback_bridge(None)
    bridge.on_play()
    assert bridge.bg_tasks == set()


# --- _mpd_safe / _on_bg_done ----------------------------------------------


@pytest.mark.asyncio
async def test_mpd_safe_returns_value() -> None:
    bridge = _callback_bridge(_mpd_client())

    async def ok() -> str:
        return "value"

    assert await bridge._mpd_safe(ok()) == "value"


@pytest.mark.asyncio
async def test_mpd_safe_swallows_command_error() -> None:
    bridge = _callback_bridge(_mpd_client())

    async def boom() -> None:
        raise mpd.CommandError("no current song")

    assert await bridge._mpd_safe(boom()) is None


@pytest.mark.asyncio
async def test_mpd_safe_swallows_connection_error() -> None:
    bridge = _callback_bridge(_mpd_client())

    async def boom() -> None:
        raise mpd.ConnectionError("lost")

    assert await bridge._mpd_safe(boom()) is None


@pytest.mark.asyncio
async def test_mpd_safe_swallows_oserror() -> None:
    bridge = _callback_bridge(_mpd_client())

    async def boom() -> None:
        raise OSError("socket gone")

    assert await bridge._mpd_safe(boom()) is None


@pytest.mark.asyncio
async def test_on_bg_done_logs_crash(caplog) -> None:
    bridge = _callback_bridge(_mpd_client())

    async def boom() -> None:
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR):
        bridge._schedule(boom())
        await _drain(bridge)
        await asyncio.sleep(0)  # let the done-callback run
    assert any("background task crashed" in r.message for r in caplog.records)
    assert bridge.bg_tasks == set()  # discarded after completion


@pytest.mark.asyncio
async def test_on_bg_done_ignores_cancelled(caplog) -> None:
    bridge = _callback_bridge(_mpd_client())

    async def forever() -> None:
        await asyncio.sleep(3600)

    bridge._schedule(forever())
    task = next(iter(bridge.bg_tasks))
    task.cancel()
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0)
    assert not any("crashed" in r.message for r in caplog.records)


# --- _reset_cover_state / refresh -----------------------------------------


def test_reset_cover_state_clears_change_detection() -> None:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._last_base = {"xesam:title": Variant("s", "x")}
    bridge._art = "file:///cache/c.jpg"
    bridge._cover_task = None
    bridge._reset_cover_state()
    assert bridge._last_base == {}
    assert bridge._art is None


@pytest.mark.asyncio
async def test_reset_cover_state_cancels_inflight_lookup() -> None:
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._last_base = {}
    bridge._art = None

    async def forever() -> None:
        await asyncio.sleep(3600)

    bridge._cover_task = asyncio.get_running_loop().create_task(forever())
    task = bridge._cover_task
    bridge._reset_cover_state()
    assert bridge._cover_task is None
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_refresh_no_client_returns_early() -> None:
    bridge = _callback_bridge(None)
    await bridge.refresh()  # no client → returns without raising


@pytest.mark.asyncio
async def test_refresh_swallows_connection_drop(caplog) -> None:
    client = MagicMock()
    client.status = AsyncMock(side_effect=mpd.ConnectionError("lost"))
    bridge = _callback_bridge(client)
    with caplog.at_level(logging.WARNING):
        await bridge.refresh()
    assert any("MPD lost during refresh" in r.message for r in caplog.records)


# --- _diff_queue -------------------------------------------------------------


def test_diff_queue_append_at_end() -> None:
    assert _diff_queue(["1", "2"], ["1", "2", "3"]) == ([("3", "2")], [])


def test_diff_queue_insert_at_start() -> None:
    assert _diff_queue(["1", "2"], ["3", "1", "2"]) == ([("3", None)], [])


def test_diff_queue_removal() -> None:
    assert _diff_queue(["1", "2", "3"], ["1", "3"]) == ([], ["2"])


def test_diff_queue_add_and_remove_order_preserved() -> None:
    added, removed = _diff_queue(["1", "2", "3"], ["1", "3", "4"])
    assert removed == ["2"]
    assert added == [("4", "3")]


def test_diff_queue_consecutive_adds_chain_predecessors() -> None:
    # Each new track's AfterTrack is the one emitted just before it.
    added, removed = _diff_queue(["1"], ["1", "2", "3"])
    assert removed == []
    assert added == [("2", "1"), ("3", "2")]


def test_diff_queue_no_change() -> None:
    assert _diff_queue(["1", "2"], ["1", "2"]) == ([], [])


def test_diff_queue_reorder_is_replace() -> None:
    assert _diff_queue(["1", "2", "3"], ["3", "2", "1"]) is None


def test_diff_queue_too_many_changes_is_replace() -> None:
    old = [str(i) for i in range(5)]
    new = [str(i) for i in range(100, 120)]
    assert _diff_queue(old, new) is None


def test_diff_queue_initial_small_population_is_adds() -> None:
    assert _diff_queue([], ["1", "2"]) == ([("1", None), ("2", "1")], [])


# --- _sync_tracklist ---------------------------------------------------------


def _tracklist_bridge(*, queue: list[dict] | None = None):
    """Bridge stub for the TrackList sync path: mocked tracklist iface,
    real ``_sync_tracklist``."""
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge.client = MagicMock()
    bridge.music_dir = Path("/srv/music")
    bridge.url_handlers = ["http://"]
    bridge.tracklist = MagicMock()
    bridge._queue = queue if queue is not None else []
    bridge._queue_version = None
    return bridge


def test_sync_tracklist_updates_tracks_property() -> None:
    bridge = _tracklist_bridge()
    bridge._sync_tracklist([{"id": "1", "title": "a"}, {"id": "2", "title": "b"}], {})
    bridge.tracklist.update_tracks.assert_called_once_with([
        "/org/mpris/MediaPlayer2/Track/1",
        "/org/mpris/MediaPlayer2/Track/2",
    ])


def test_sync_tracklist_emits_added_with_metadata() -> None:
    bridge = _tracklist_bridge(queue=[{"id": "1", "title": "a", "pos": "0"}])
    bridge._sync_tracklist(
        [{"id": "1", "title": "a", "pos": "0"},
         {"id": "2", "title": "b", "pos": "1"}],
        {},
    )
    (meta, after), _ = bridge.tracklist.emit_track_added.call_args
    assert meta["xesam:title"].value == "b"
    assert meta["mpris:trackid"].value == "/org/mpris/MediaPlayer2/Track/2"
    assert after == "/org/mpris/MediaPlayer2/Track/1"
    bridge.tracklist.emit_track_removed.assert_not_called()
    bridge.tracklist.emit_track_list_replaced.assert_not_called()


def test_sync_tracklist_first_add_is_after_no_track() -> None:
    bridge = _tracklist_bridge()
    bridge._sync_tracklist([{"id": "5", "title": "x"}], {})
    (_, after), _ = bridge.tracklist.emit_track_added.call_args
    assert after == NO_TRACK


def test_sync_tracklist_emits_removed() -> None:
    bridge = _tracklist_bridge(
        queue=[{"id": "1", "title": "a", "pos": "0"},
               {"id": "2", "title": "b", "pos": "1"}],
    )
    bridge._sync_tracklist([{"id": "2", "title": "b", "pos": "0"}], {})
    bridge.tracklist.emit_track_removed.assert_called_once_with(
        "/org/mpris/MediaPlayer2/Track/1")
    bridge.tracklist.emit_track_added.assert_not_called()
    # pos shifted 1 -> 0 but tags are identical: no metadata-changed signal
    bridge.tracklist.emit_track_metadata_changed.assert_not_called()


def test_sync_tracklist_reorder_emits_replaced_with_current() -> None:
    bridge = _tracklist_bridge(
        queue=[{"id": "1", "title": "a"}, {"id": "2", "title": "b"}],
    )
    bridge._sync_tracklist(
        [{"id": "2", "title": "b"}, {"id": "1", "title": "a"}],
        {"songid": "2"},
    )
    bridge.tracklist.emit_track_list_replaced.assert_called_once_with(
        "/org/mpris/MediaPlayer2/Track/2")
    bridge.tracklist.emit_track_added.assert_not_called()
    bridge.tracklist.emit_track_removed.assert_not_called()


def test_sync_tracklist_replaced_without_current_uses_no_track() -> None:
    bridge = _tracklist_bridge(
        queue=[{"id": str(i)} for i in range(20)],
    )
    bridge._sync_tracklist([{"id": str(i)} for i in range(100, 120)], {})
    bridge.tracklist.emit_track_list_replaced.assert_called_once_with(NO_TRACK)


def test_sync_tracklist_icy_title_change_emits_metadata_changed() -> None:
    bridge = _tracklist_bridge(
        queue=[{"id": "1", "file": "http://r/x.mp3", "title": "Old - Song", "pos": "0"}],
    )
    bridge._sync_tracklist(
        [{"id": "1", "file": "http://r/x.mp3", "title": "New - Song", "pos": "0"}],
        {},
    )
    (tid, meta), _ = bridge.tracklist.emit_track_metadata_changed.call_args
    assert tid == "/org/mpris/MediaPlayer2/Track/1"
    assert meta["xesam:title"].value == "New - Song"


# --- _refresh_tracklist ------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_tracklist_skips_when_version_unchanged() -> None:
    bridge = _tracklist_bridge()
    bridge._queue_version = "12"
    bridge.client.playlistinfo = AsyncMock()
    await bridge._refresh_tracklist({"playlist": "12"})
    bridge.client.playlistinfo.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_tracklist_fetches_on_version_change() -> None:
    bridge = _tracklist_bridge()
    bridge._queue_version = "12"
    bridge.client.playlistinfo = AsyncMock(return_value=[{"id": "1", "title": "a"}])
    await bridge._refresh_tracklist({"playlist": "13"})
    assert bridge._queue_version == "13"
    assert bridge._queue == [{"id": "1", "title": "a"}]
    bridge.tracklist.update_tracks.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_tracklist_keeps_version_on_error() -> None:
    # A failed playlistinfo must not advance the version, so the fetch is
    # retried on the next refresh.
    bridge = _tracklist_bridge()
    bridge._queue_version = "12"
    bridge.client.playlistinfo = AsyncMock(side_effect=mpd.ConnectionError("lost"))
    await bridge._refresh_tracklist({"playlist": "13"})
    assert bridge._queue_version == "12"
    bridge.tracklist.update_tracks.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tracklist_no_client_is_noop() -> None:
    bridge = _tracklist_bridge()
    bridge.client = None
    await bridge._refresh_tracklist({"playlist": "1"})
    bridge.tracklist.update_tracks.assert_not_called()


def test_reset_tracklist_state_empties_queue() -> None:
    bridge = _tracklist_bridge(queue=[{"id": "1"}])
    bridge._queue_version = "3"
    bridge._reset_tracklist_state()
    assert bridge._queue == []
    assert bridge._queue_version is None
    bridge.tracklist.update_tracks.assert_called_once_with([])
    bridge.tracklist.emit_track_list_replaced.assert_called_once_with(NO_TRACK)


def test_reset_tracklist_state_already_empty_stays_silent() -> None:
    bridge = _tracklist_bridge()
    bridge._reset_tracklist_state()
    bridge.tracklist.update_tracks.assert_not_called()
    bridge.tracklist.emit_track_list_replaced.assert_not_called()


# --- TrackList callbacks -----------------------------------------------------


def _tracklist_client():
    c = MagicMock()
    for name in ("playid", "deleteid", "addid"):
        setattr(c, name, AsyncMock())
    c.addid.return_value = "9"
    return c


def _tracklist_callback_bridge(client, *, queue: list[dict] | None = None):
    bridge = MpdMprisBridge.__new__(MpdMprisBridge)
    bridge._loop = asyncio.get_running_loop()
    bridge.bg_tasks = set()
    bridge.client = client
    bridge.music_dir = Path("/srv/music")
    bridge.url_handlers = ["http://"]
    bridge._queue = queue if queue is not None else []
    return bridge


@pytest.mark.asyncio
async def test_on_tracklist_goto_plays_songid() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    bridge.on_tracklist_goto("/org/mpris/MediaPlayer2/Track/7")
    await _drain(bridge)
    client.playid.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_on_tracklist_goto_foreign_path_is_noop() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    bridge.on_tracklist_goto(NO_TRACK)
    await _drain(bridge)
    client.playid.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_tracklist_remove_deletes_songid() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    bridge.on_tracklist_remove("/org/mpris/MediaPlayer2/Track/3")
    await _drain(bridge)
    client.deleteid.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_on_tracklist_add_at_queue_start() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    bridge.on_tracklist_add("http://stream/x.mp3", NO_TRACK, False)
    await _drain(bridge)
    client.addid.assert_awaited_once_with("http://stream/x.mp3", 0)
    client.playid.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_tracklist_add_after_known_track() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(
        client, queue=[{"id": "1", "pos": "0"}, {"id": "2", "pos": "1"}],
    )
    bridge.on_tracklist_add(
        "file:///srv/music/a/b.flac", "/org/mpris/MediaPlayer2/Track/1", False,
    )
    await _drain(bridge)
    client.addid.assert_awaited_once_with("a/b.flac", 1)


@pytest.mark.asyncio
async def test_on_tracklist_add_unknown_after_appends() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client, queue=[{"id": "1"}])
    bridge.on_tracklist_add(
        "http://stream/x.mp3", "/org/mpris/MediaPlayer2/Track/99", False,
    )
    await _drain(bridge)
    client.addid.assert_awaited_once_with("http://stream/x.mp3")


@pytest.mark.asyncio
async def test_on_tracklist_add_set_as_current_plays_new_id() -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    bridge.on_tracklist_add("http://stream/x.mp3", NO_TRACK, True)
    await _drain(bridge)
    client.playid.assert_awaited_once_with(9)


@pytest.mark.asyncio
async def test_on_tracklist_add_unmappable_uri_is_noop(caplog) -> None:
    client = _tracklist_client()
    bridge = _tracklist_callback_bridge(client)
    with caplog.at_level(logging.WARNING):
        bridge.on_tracklist_add("file:///outside/library.mp3", NO_TRACK, False)
    await _drain(bridge)
    client.addid.assert_not_awaited()
    assert any("AddTrack" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_on_tracklist_add_no_client_is_noop() -> None:
    bridge = _tracklist_callback_bridge(None)
    bridge.on_tracklist_add("http://stream/x.mp3", NO_TRACK, False)
    assert bridge.bg_tasks == set()


@pytest.mark.asyncio
async def test_on_get_tracks_metadata_from_cached_queue() -> None:
    bridge = _tracklist_callback_bridge(
        _tracklist_client(),
        queue=[{"id": "1", "title": "a"}, {"id": "2", "title": "b"}],
    )
    out = bridge.on_get_tracks_metadata([
        "/org/mpris/MediaPlayer2/Track/2",
        "/org/mpris/MediaPlayer2/Track/99",  # unknown: omitted per spec
    ])
    assert len(out) == 1
    assert out[0]["xesam:title"].value == "b"
    assert out[0]["mpris:trackid"].value == "/org/mpris/MediaPlayer2/Track/2"
