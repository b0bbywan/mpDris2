"""MPRIS2 D-Bus interface, exposed via dbus-fast.

Three ServiceInterface subclasses correspond to the interfaces we
implement on the object path ``/org/mpris/MediaPlayer2``:

* ``org.mpris.MediaPlayer2``           — identity + capabilities (root)
* ``org.mpris.MediaPlayer2.Player``    — playback state + controls
* ``org.mpris.MediaPlayer2.TrackList`` — the play queue (optional per spec)

Behaviour is driven from the outside: callbacks injected at construction
time handle Play/Pause/Stop/PlayPause/Next/Previous/Seek/SetPosition/
volume/loop/shuffle, and ``update_*`` push state changes back to
subscribed MPRIS clients via ``emit_properties_changed``.

This module has no MPD knowledge — see ``mpd2mpris.bridge`` for the glue.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from dbus_fast.errors import DBusError
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"

logger = logging.getLogger(__name__)

ROOT_PATH = "/org/mpris/MediaPlayer2"
MEDIA_PLAYER_IFACE = "org.mpris.MediaPlayer2"
BUS_NAME = f"{MEDIA_PLAYER_IFACE}.mpd"
PLAYER_IFACE = f"{MEDIA_PLAYER_IFACE}.Player"
TRACKLIST_IFACE = f"{MEDIA_PLAYER_IFACE}.TrackList"

IDENTITY = "Music Player Daemon"

VALID_PLAYBACK_STATUS = {"Playing", "Paused", "Stopped"}
VALID_LOOP_STATUS = {"None", "Track", "Playlist"}


class MediaPlayer2(ServiceInterface):
    """Root MPRIS interface — identity + capabilities."""

    def __init__(self) -> None:
        super().__init__(MEDIA_PLAYER_IFACE)

    @method()
    def Raise(self):  # noqa: N802
        # MPD has no GUI to bring forward; advertise CanRaise=False and
        # answer the method with NotSupported per MPRIS spec hints.
        raise DBusError(NOT_SUPPORTED, "Raise is not supported")

    @method()
    def Quit(self):  # noqa: N802
        raise DBusError(NOT_SUPPORTED, "Quit is not supported")

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":  # noqa: N802
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":  # noqa: N802
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":  # noqa: N802
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":  # noqa: N802
        return IDENTITY

    # No DesktopEntry property: we no longer ship a ``mpd2mpris.desktop``
    # (D-Bus activation handles launch), so advertising one would dangle.

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":  # noqa: N802
        # Filled at daemon startup from MPD's ``urlhandlers`` command if
        # we ever want to advertise OpenUri. For now MPD's local URI
        # scheme isn't MPRIS-portable, so leave empty.
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":  # noqa: N802
        return []


class MediaPlayer2Player(ServiceInterface):
    """Player MPRIS interface — playback state, metadata and controls."""

    def __init__(
        self,
        on_play: Callable[[], None] | None = None,
        on_pause: Callable[[], None] | None = None,
        on_play_pause: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_next: Callable[[], None] | None = None,
        on_previous: Callable[[], None] | None = None,
        on_seek: Callable[[int], None] | None = None,
        on_set_position: Callable[[str, int], None] | None = None,
        on_volume_set: Callable[[float], None] | None = None,
        on_loop_status_set: Callable[[str], None] | None = None,
        on_shuffle_set: Callable[[bool], None] | None = None,
        on_get_position: Callable[[], Awaitable[int | None]] | None = None,
    ) -> None:
        super().__init__(PLAYER_IFACE)
        self._playback_status = "Stopped"
        self._loop_status = "None"
        self._shuffle = False
        self._metadata: dict = {}
        self._volume = 0.0
        self._position = 0          # microseconds, int64
        # Capabilities — filled in from MPD state on every refresh.
        self._can_play = True
        self._can_pause = True
        self._can_go_next = True
        self._can_go_previous = True
        self._can_seek = False      # flips True once we have mpris:length
        self._on_play = on_play
        self._on_pause = on_pause
        self._on_play_pause = on_play_pause
        self._on_stop = on_stop
        self._on_next = on_next
        self._on_previous = on_previous
        self._on_seek = on_seek
        self._on_set_position = on_set_position
        self._on_volume_set = on_volume_set
        self._on_loop_status_set = on_loop_status_set
        self._on_shuffle_set = on_shuffle_set
        self._on_get_position = on_get_position

    # --- MPRIS methods ------------------------------------------------
    @method()
    def Play(self):  # noqa: N802
        if self._on_play:
            self._on_play()

    @method()
    def Pause(self):  # noqa: N802
        if self._on_pause:
            self._on_pause()

    @method()
    def PlayPause(self):  # noqa: N802
        if self._on_play_pause:
            self._on_play_pause()

    @method()
    def Stop(self):  # noqa: N802
        if self._on_stop:
            self._on_stop()

    @method()
    def Next(self):  # noqa: N802
        if self._on_next:
            self._on_next()

    @method()
    def Previous(self):  # noqa: N802
        if self._on_previous:
            self._on_previous()

    @method()
    def Seek(self, Offset: "x"):  # noqa: N802, N803
        if self._on_seek:
            self._on_seek(int(Offset))

    @method()
    def SetPosition(self, TrackId: "o", Position: "x"):  # noqa: N802, N803
        if self._on_set_position:
            self._on_set_position(str(TrackId), int(Position))

    @method()
    def OpenUri(self, Uri: "s"):  # noqa: N802, N803, ARG002
        raise DBusError(NOT_SUPPORTED, "OpenUri is not supported")

    @signal()
    def Seeked(self, Position: "x") -> "x":  # noqa: N802, N803
        return Position

    # --- MPRIS properties --------------------------------------------
    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":  # noqa: N802
        return self._playback_status

    @dbus_property()
    def LoopStatus(self) -> "s":  # noqa: N802
        return self._loop_status

    @LoopStatus.setter  # type: ignore[no-redef]
    def LoopStatus(self, val: "s") -> None:  # noqa: N802
        if val not in VALID_LOOP_STATUS:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs",
                            f"LoopStatus {val!r} is not a valid value")
        if self._on_loop_status_set:
            self._on_loop_status_set(val)

    @dbus_property()
    def Shuffle(self) -> "b":  # noqa: N802
        return self._shuffle

    @Shuffle.setter  # type: ignore[no-redef]
    def Shuffle(self, val: "b") -> None:  # noqa: N802
        if self._on_shuffle_set:
            self._on_shuffle_set(bool(val))

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":  # noqa: N802
        return self._metadata

    @dbus_property()
    def Volume(self) -> "d":  # noqa: N802
        return self._volume

    @Volume.setter  # type: ignore[no-redef]
    def Volume(self, val: "d") -> None:  # noqa: N802
        clamped = max(0.0, min(1.0, float(val)))
        logger.debug("MPRIS Set Volume: %.3f -> %.3f", self._volume, clamped)
        if clamped == self._volume:
            if self._on_volume_set:
                self._on_volume_set(clamped)
            return
        self._volume = clamped
        # Emit synchronously so every MPRIS subscriber learns about the
        # change immediately. The follow-up MPD idle refresh will early-
        # return in update_volume() because self._volume already matches.
        self.emit_properties_changed({"Volume": clamped})
        if self._on_volume_set:
            self._on_volume_set(clamped)

    @dbus_property(access=PropertyAccess.READ)
    async def Position(self) -> "x":  # noqa: N802
        # MPD is the source of truth: the cached value only advances on the
        # idle-driven refresh, so a client polling between events would see
        # a frozen position. Ask MPD live and fall back to the cache when
        # there's no live connection.
        if self._on_get_position is not None:
            live = await self._on_get_position()
            if live is not None:
                self._position = live
        return self._position

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":  # noqa: N802
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":  # noqa: N802
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":  # noqa: N802
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":  # noqa: N802
        return self._can_go_next

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":  # noqa: N802
        return self._can_go_previous

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":  # noqa: N802
        return self._can_play

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":  # noqa: N802
        return self._can_pause

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":  # noqa: N802
        return self._can_seek

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":  # noqa: N802
        # Hardcoded True: an MPD bridge is always controllable in principle.
        # The per-action Can* flags reflect the playlist state more
        # precisely (e.g. CanGoNext = there is a next song).
        return True

    # --- External update API -----------------------------------------
    def update_playback_status(self, status: str) -> None:
        if status not in VALID_PLAYBACK_STATUS:
            logger.warning("ignoring invalid playback status: %s", status)
            return
        if status == self._playback_status:
            return
        self._playback_status = status
        self.emit_properties_changed({"PlaybackStatus": status})

    def update_loop_status(self, status: str) -> None:
        if status not in VALID_LOOP_STATUS:
            logger.warning("ignoring invalid loop status: %s", status)
            return
        if status == self._loop_status:
            return
        self._loop_status = status
        self.emit_properties_changed({"LoopStatus": status})

    def update_shuffle(self, shuffle: bool) -> None:
        shuffle = bool(shuffle)
        if shuffle == self._shuffle:
            return
        self._shuffle = shuffle
        self.emit_properties_changed({"Shuffle": shuffle})

    def update_metadata(self, metadata: dict) -> None:
        # Always replace + emit even if identical — MPRIS clients can
        # rely on a Metadata signal after every track change. Cheap.
        self._metadata = metadata
        self.emit_properties_changed({"Metadata": metadata})

    def update_volume(self, volume: float) -> None:
        clamped = max(0.0, min(1.0, float(volume)))
        if clamped == self._volume:
            return
        self._volume = clamped
        self.emit_properties_changed({"Volume": clamped})

    def update_position(self, position_us: int) -> None:
        # Position is not emitted via PropertiesChanged per spec
        # (it changes continuously); stored for Get(Position) reads
        # and used as a baseline by Seeked emission in the daemon.
        self._position = int(position_us)

    def update_capabilities(self, *, can_play: bool | None = None,
                            can_pause: bool | None = None,
                            can_go_next: bool | None = None,
                            can_go_previous: bool | None = None,
                            can_seek: bool | None = None) -> None:
        changed: dict = {}
        if can_play is not None and can_play != self._can_play:
            self._can_play = can_play
            changed["CanPlay"] = can_play
        if can_pause is not None and can_pause != self._can_pause:
            self._can_pause = can_pause
            changed["CanPause"] = can_pause
        if can_go_next is not None and can_go_next != self._can_go_next:
            self._can_go_next = can_go_next
            changed["CanGoNext"] = can_go_next
        if can_go_previous is not None and can_go_previous != self._can_go_previous:
            self._can_go_previous = can_go_previous
            changed["CanGoPrevious"] = can_go_previous
        if can_seek is not None and can_seek != self._can_seek:
            self._can_seek = can_seek
            changed["CanSeek"] = can_seek
        if changed:
            self.emit_properties_changed(changed)

    def emit_seeked(self, position_us: int) -> None:
        self._position = int(position_us)
        self.Seeked(int(position_us))


class MediaPlayer2TrackList(ServiceInterface):
    """TrackList MPRIS interface — the play queue as track object paths.

    Like the Player interface, behaviour is injected: callbacks handle
    GoTo/AddTrack/RemoveTrack/GetTracksMetadata, and the ``update_*`` /
    ``emit_*`` methods push queue changes out to MPRIS clients."""

    def __init__(
        self,
        on_go_to: Callable[[str], None] | None = None,
        on_add_track: Callable[[str, str, bool], None] | None = None,
        on_remove_track: Callable[[str], None] | None = None,
        on_get_tracks_metadata: Callable[[list[str]], list[dict]] | None = None,
    ) -> None:
        super().__init__(TRACKLIST_IFACE)
        self._tracks: list[str] = []
        self._can_edit = True
        self._on_go_to = on_go_to
        self._on_add_track = on_add_track
        self._on_remove_track = on_remove_track
        self._on_get_tracks_metadata = on_get_tracks_metadata

    # --- MPRIS methods ------------------------------------------------
    @method()
    def GetTracksMetadata(self, TrackIds: "ao") -> "aa{sv}":  # noqa: N802, N803
        if self._on_get_tracks_metadata is None:
            return []
        return self._on_get_tracks_metadata([str(t) for t in TrackIds])

    @method()
    def AddTrack(self, Uri: "s", AfterTrack: "o", SetAsCurrent: "b"):  # noqa: N802, N803
        if self._on_add_track:
            self._on_add_track(str(Uri), str(AfterTrack), bool(SetAsCurrent))

    @method()
    def RemoveTrack(self, TrackId: "o"):  # noqa: N802, N803
        if self._on_remove_track:
            self._on_remove_track(str(TrackId))

    @method()
    def GoTo(self, TrackId: "o"):  # noqa: N802, N803
        if self._on_go_to:
            self._on_go_to(str(TrackId))

    # --- MPRIS signals -------------------------------------------------
    @signal()
    def TrackListReplaced(self, Tracks: "ao", CurrentTrack: "o") -> "aoo":  # noqa: N802, N803
        return [Tracks, CurrentTrack]

    @signal()
    def TrackAdded(self, Metadata: "a{sv}", AfterTrack: "o") -> "a{sv}o":  # noqa: N802, N803
        return [Metadata, AfterTrack]

    @signal()
    def TrackRemoved(self, TrackId: "o") -> "o":  # noqa: N802, N803
        return TrackId

    @signal()
    def TrackMetadataChanged(self, TrackId: "o", Metadata: "a{sv}") -> "oa{sv}":  # noqa: N802, N803
        return [TrackId, Metadata]

    # --- MPRIS properties ----------------------------------------------
    @dbus_property(access=PropertyAccess.READ)
    def Tracks(self) -> "ao":  # noqa: N802
        return self._tracks

    @dbus_property(access=PropertyAccess.READ)
    def CanEditTracks(self) -> "b":  # noqa: N802
        return self._can_edit

    # --- External update API -------------------------------------------
    def update_tracks(self, tracks: list[str]) -> None:
        if tracks == self._tracks:
            return
        self._tracks = list(tracks)
        # Spec: Tracks changes are announced invalidated-only (the list
        # can be huge); the accompanying Track* signals carry the detail.
        self.emit_properties_changed({}, ["Tracks"])

    def update_can_edit(self, can_edit: bool) -> None:
        can_edit = bool(can_edit)
        if can_edit == self._can_edit:
            return
        self._can_edit = can_edit
        self.emit_properties_changed({"CanEditTracks": can_edit})

    def emit_track_list_replaced(self, current: str) -> None:
        self.TrackListReplaced(self._tracks, current)

    def emit_track_added(self, metadata: dict, after: str) -> None:
        self.TrackAdded(metadata, after)

    def emit_track_removed(self, trackid: str) -> None:
        self.TrackRemoved(trackid)

    def emit_track_metadata_changed(self, trackid: str, metadata: dict) -> None:
        self.TrackMetadataChanged(trackid, metadata)
