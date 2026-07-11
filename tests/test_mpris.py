"""ServiceInterface state-machine tests — no real D-Bus.

dbus-fast lets us instantiate ServiceInterface subclasses without
exporting them on a bus; emit_properties_changed is a no-op until the
object is attached to a connected ``MessageBus``. That's enough to
cover the update_* contract and the backend-callback dispatch.
"""

from __future__ import annotations

import pytest
from dbus_fast.errors import DBusError

from mpd2mpris.mpris import (
    MediaPlayer2,
    MediaPlayer2Player,
    MediaPlayer2Playlists,
    MediaPlayer2TrackList,
)

# dbus-fast `@dbus_property` rewrites the decorated function into a
# regular attribute (the descriptor returns the stored value on read),
# so tests dereference these without calling them: `p.Volume`, not
# `p.Volume()`. `@method`-decorated functions stay callable normally.


def test_root_identity() -> None:
    root = MediaPlayer2()
    assert root.Identity == "Music Player Daemon"
    assert root.CanQuit is False
    assert root.CanRaise is False
    assert root.HasTrackList is True


def test_player_defaults() -> None:
    p = MediaPlayer2Player()
    assert p.PlaybackStatus == "Stopped"
    assert p.LoopStatus == "None"
    assert p.Shuffle is False
    assert p.Metadata == {}
    assert p.Volume == 0.0
    assert p.CanControl is True
    assert p.CanSeek is False


@pytest.mark.asyncio
async def test_position_defaults_to_cache_without_callback() -> None:
    p = MediaPlayer2Player()
    assert await p.Position == 0


@pytest.mark.asyncio
async def test_position_queries_backend_live() -> None:
    p = MediaPlayer2Player(on_get_position=lambda: _aval(7_500_000))
    assert await p.Position == 7_500_000


@pytest.mark.asyncio
async def test_position_falls_back_to_cache_when_backend_none() -> None:
    p = MediaPlayer2Player(on_get_position=lambda: _aval(None))
    p.update_position(3_000_000)
    assert await p.Position == 3_000_000


async def _aval(v: int | None) -> int | None:
    return v


def test_update_playback_status_valid() -> None:
    p = MediaPlayer2Player()
    p.update_playback_status("Playing")
    assert p.PlaybackStatus == "Playing"
    p.update_playback_status("Paused")
    assert p.PlaybackStatus == "Paused"


def test_update_playback_status_invalid_is_ignored() -> None:
    p = MediaPlayer2Player()
    p.update_playback_status("BogusValue")
    assert p.PlaybackStatus == "Stopped"


def test_update_loop_status() -> None:
    p = MediaPlayer2Player()
    p.update_loop_status("Track")
    assert p.LoopStatus == "Track"
    p.update_loop_status("Playlist")
    assert p.LoopStatus == "Playlist"
    p.update_loop_status("Invalid")
    assert p.LoopStatus == "Playlist"  # unchanged


def test_update_volume_clamps() -> None:
    p = MediaPlayer2Player()
    p.update_volume(1.5)
    assert p.Volume == 1.0
    p.update_volume(-0.2)
    assert p.Volume == 0.0
    p.update_volume(0.5)
    assert p.Volume == 0.5


def test_update_capabilities_changes_only() -> None:
    p = MediaPlayer2Player()
    p.update_capabilities(can_seek=True)
    assert p.CanSeek is True
    p.update_capabilities(can_go_next=False, can_seek=True)  # can_seek unchanged
    assert p.CanGoNext is False
    assert p.CanSeek is True


def test_backend_callbacks_fire() -> None:
    calls: list[str] = []
    p = MediaPlayer2Player(
        on_play=lambda: calls.append("play"),
        on_pause=lambda: calls.append("pause"),
        on_play_pause=lambda: calls.append("toggle"),
        on_stop=lambda: calls.append("stop"),
        on_next=lambda: calls.append("next"),
        on_previous=lambda: calls.append("prev"),
        on_seek=lambda us: calls.append(f"seek:{us}"),
        on_set_position=lambda tid, us: calls.append(f"setpos:{tid}:{us}"),
        on_volume_set=lambda v: calls.append(f"vol:{v}"),
        on_loop_status_set=lambda v: calls.append(f"loop:{v}"),
        on_shuffle_set=lambda v: calls.append(f"shuffle:{v}"),
    )
    p.Play()
    p.Pause()
    p.PlayPause()
    p.Stop()
    p.Next()
    p.Previous()
    p.Seek(1_000_000)
    p.SetPosition("/org/mpris/MediaPlayer2/Track/42", 5_000_000)
    p.Volume = 0.75  # type: ignore[misc, assignment]
    p.LoopStatus = "Track"  # type: ignore[misc, assignment]
    p.Shuffle = True  # type: ignore[misc, assignment]
    assert calls == [
        "play", "pause", "toggle", "stop", "next", "prev",
        "seek:1000000",
        "setpos:/org/mpris/MediaPlayer2/Track/42:5000000",
        "vol:0.75",
        "loop:Track",
        "shuffle:True",
    ]


def test_volume_setter_emits_synchronously() -> None:
    """Volume setter mutates state before invoking the backend callback,
    so the synchronous PropertiesChanged emit (a no-op here, no bus) has
    the new value in self._volume."""
    seen = []
    p = MediaPlayer2Player(on_volume_set=lambda v: seen.append(p._volume))
    p.Volume = 0.4  # type: ignore[misc, assignment]
    assert seen == [0.4]
    assert p.Volume == 0.4


def test_invalid_loop_status_setter_raises() -> None:
    p = MediaPlayer2Player(on_loop_status_set=lambda v: None)
    with pytest.raises(DBusError):
        p.LoopStatus = "Garbage"  # type: ignore[misc, assignment]


# --- MediaPlayer2TrackList --------------------------------------------------

def test_tracklist_defaults() -> None:
    tl = MediaPlayer2TrackList()
    assert tl.Tracks == []
    assert tl.CanEditTracks is True


def test_tracklist_callbacks_fire() -> None:
    calls: list[str] = []
    tl = MediaPlayer2TrackList(
        on_go_to=lambda tid: calls.append(f"goto:{tid}"),
        on_add_track=lambda uri, after, cur: calls.append(f"add:{uri}:{after}:{cur}"),
        on_remove_track=lambda tid: calls.append(f"remove:{tid}"),
    )
    tl.GoTo("/org/mpris/MediaPlayer2/Track/3")
    tl.AddTrack("http://x/y.mp3", "/org/mpris/MediaPlayer2/TrackList/NoTrack", True)
    tl.RemoveTrack("/org/mpris/MediaPlayer2/Track/3")
    assert calls == [
        "goto:/org/mpris/MediaPlayer2/Track/3",
        "add:http://x/y.mp3:/org/mpris/MediaPlayer2/TrackList/NoTrack:True",
        "remove:/org/mpris/MediaPlayer2/Track/3",
    ]


# dbus-fast's `@method` wrapper drops the return value when called
# directly (the D-Bus dispatch uses the original function, kept in
# ``__wrapped__``) — so returning methods are tested via ``__wrapped__``.

def test_tracklist_get_tracks_metadata_dispatches() -> None:
    tl = MediaPlayer2TrackList(
        on_get_tracks_metadata=lambda ids: [{"asked": ids}],
    )
    ids = ["/org/mpris/MediaPlayer2/Track/1", "/org/mpris/MediaPlayer2/Track/2"]
    assert tl.GetTracksMetadata.__wrapped__(tl, ids) == [{"asked": ids}]


def test_tracklist_get_tracks_metadata_without_callback() -> None:
    tl = MediaPlayer2TrackList()
    assert tl.GetTracksMetadata.__wrapped__(
        tl, ["/org/mpris/MediaPlayer2/Track/1"],
    ) == []


def test_tracklist_update_tracks() -> None:
    tl = MediaPlayer2TrackList()
    paths = ["/org/mpris/MediaPlayer2/Track/1", "/org/mpris/MediaPlayer2/Track/2"]
    tl.update_tracks(paths)
    assert tl.Tracks == paths
    tl.update_tracks(paths)  # same value: no-op, still equal
    assert tl.Tracks == paths


def test_tracklist_update_can_edit() -> None:
    tl = MediaPlayer2TrackList()
    tl.update_can_edit(False)
    assert tl.CanEditTracks is False
    tl.update_can_edit(True)
    assert tl.CanEditTracks is True


# --- MediaPlayer2Playlists ---------------------------------------------------

def test_playlists_defaults() -> None:
    pl = MediaPlayer2Playlists()
    assert pl.PlaylistCount == 0
    assert pl.Orderings == ["Alphabetical", "Modified"]
    assert pl.ActivePlaylist == [False, ["/", "", ""]]


def test_playlists_activate_dispatches() -> None:
    calls: list[str] = []
    pl = MediaPlayer2Playlists(on_activate_playlist=calls.append)
    pl.ActivatePlaylist("/org/mpris/MediaPlayer2/Playlist/jazz")
    assert calls == ["/org/mpris/MediaPlayer2/Playlist/jazz"]


def test_playlists_get_playlists_dispatches() -> None:
    pl = MediaPlayer2Playlists(
        on_get_playlists=lambda i, n, order, rev: [["/pl", f"{i}:{n}:{order}:{rev}", ""]],
    )
    out = pl.GetPlaylists.__wrapped__(pl, 5, 20, "Alphabetical", True)
    assert out == [["/pl", "5:20:Alphabetical:True", ""]]


def test_playlists_get_playlists_without_callback() -> None:
    pl = MediaPlayer2Playlists()
    assert pl.GetPlaylists.__wrapped__(pl, 0, 10, "Alphabetical", False) == []


def test_playlists_update_count() -> None:
    pl = MediaPlayer2Playlists()
    pl.update_playlist_count(4)
    assert pl.PlaylistCount == 4


def test_playlists_update_active() -> None:
    pl = MediaPlayer2Playlists()
    pl.update_active_playlist(("/org/mpris/MediaPlayer2/Playlist/jazz", "jazz", ""))
    assert pl.ActivePlaylist == [
        True, ["/org/mpris/MediaPlayer2/Playlist/jazz", "jazz", ""],
    ]
    pl.update_active_playlist(None)
    assert pl.ActivePlaylist == [False, ["/", "", ""]]
