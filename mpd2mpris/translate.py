"""Pure MPD → MPRIS shape conversions.

No D-Bus, no asyncio, no I/O — just shape conversion + tag mapping +
``dbus_fast.Variant`` wrapping. Covers both currentsong() → MPRIS
Metadata (``mpd_to_mpris``) and the smaller per-field status() helpers
(``parse_volume``, ``parse_elapsed``, ``playback_status_from``,
``loop_status_from``) the bridge needs on every refresh.

Keeping these side-effect-free makes them trivial to unit-test and
reusable: cover lookup, for instance, runs separately and adds
``mpris:artUrl`` on top of ``mpd_to_mpris``'s result.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dbus_fast import Variant

# Tags whose MPD value may legitimately be a list (multiple artists,
# multiple genres, …). For single-valued MPD tags we still wrap as a
# list when the MPRIS key is `as`-typed.
_LIST_TAGS = frozenset({"artist", "albumartist", "composer", "genre"})

# Default URL schemes recognised as "already a URL"; daemon overrides
# this from MPD's ``urlhandlers`` command at startup when available.
DEFAULT_URL_HANDLERS = ("http://", "https://", "mms://", "cdda://", "file://")

# MPRIS track object paths: one per MPD queue entry, keyed by songid.
TRACK_ID_PREFIX = "/org/mpris/MediaPlayer2/Track/"
# TrackList sentinel: "no track" (start-of-queue for AddTrack, empty
# CurrentTrack in TrackListReplaced).
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

# MPRIS playlist object paths, one per MPD stored playlist. D-Bus
# object paths only allow ``[A-Za-z0-9_]`` per element, so the name is
# hex-escaped (see ``playlist_id``).
PLAYLIST_ID_PREFIX = "/org/mpris/MediaPlayer2/Playlist/"


def _to_list(val: object) -> list[str]:
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def first(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def track_id(songid: object) -> str:
    """MPD songid -> MPRIS track object path: ``12`` gives
    ``/org/mpris/MediaPlayer2/Track/12``."""
    return f"{TRACK_ID_PREFIX}{first(songid)}"


def songid_from(trackid: str) -> int | None:
    """MPRIS track object path -> MPD songid:
    ``/org/mpris/MediaPlayer2/Track/12`` gives ``12``; ``NO_TRACK`` and
    foreign paths give ``None``."""
    if not trackid.startswith(TRACK_ID_PREFIX):
        return None
    try:
        return int(trackid[len(TRACK_ID_PREFIX):])
    except ValueError:
        return None


def playlist_id(name: str) -> str:
    """MPD playlist name -> MPRIS playlist object path: ``jazz 2024``
    gives ``/org/mpris/MediaPlayer2/Playlist/jazz_202024``. ASCII
    letters and digits pass through; every other UTF-8 byte (``_``
    included) becomes ``_XX`` hex so the mapping is reversible."""
    out = []
    for b in name.encode("utf-8"):
        if 0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A:
            out.append(chr(b))
        else:
            out.append(f"_{b:02X}")
    return PLAYLIST_ID_PREFIX + "".join(out)


def playlist_name_from(playlist_path: str) -> str | None:
    """Inverse of ``playlist_id``:
    ``/org/mpris/MediaPlayer2/Playlist/jazz_202024`` gives ``jazz 2024``.
    Foreign or malformed paths (bad hex, invalid UTF-8) give ``None``."""
    if not playlist_path.startswith(PLAYLIST_ID_PREFIX):
        return None
    encoded = playlist_path[len(PLAYLIST_ID_PREFIX):]
    if not encoded:
        return None
    raw = bytearray()
    i = 0
    while i < len(encoded):
        if encoded[i] == "_":
            if i + 3 > len(encoded):
                return None
            try:
                raw.append(int(encoded[i + 1:i + 3], 16))
            except ValueError:
                return None
            i += 3
        else:
            raw.append(ord(encoded[i]))
            i += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# Spaced dash between artist and track: ASCII hyphen, en-dash or em-dash.
# Spaces on both sides are required so hyphenated names ("Jean-Luc") aren't split.
_TITLE_SEP = re.compile(r"\s+[-–—]\s+")


def split_title(title: str) -> tuple[str, str] | None:
    """Web-radio ICY titles are usually ``Artist - Track``. Split on the first
    spaced dash (ASCII ``-``, en-dash ``–`` or em-dash ``—``);
    ``None`` when there's no separator (jingles, promos, bare station names) so
    callers never query a backend with junk."""
    parts = _TITLE_SEP.split(title, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, track = parts[0].strip(), parts[1].strip()
    if not artist or not track:
        return None
    return artist, track


_NORM = re.compile(r"[^a-z0-9]+")


def normalize(s: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics to a single space and
    trim — a coarse key for loose, punctuation-insensitive matching."""
    return _NORM.sub(" ", s.lower()).strip()


def artist_matches(query: str, candidate: str) -> bool:
    """Loose containment match (via ``normalize``) — enough to confirm a
    search hit is the right artist without over-rejecting. Empty never
    matches."""
    q, c = normalize(query), normalize(candidate)
    return bool(q) and bool(c) and (q == c or q in c or c in q)


def _parse_leading_int(s: str) -> int | None:
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


# --- status() helpers -----------------------------------------------------


def playback_status_from(state: str) -> str:
    """MPD ``state`` -> MPRIS ``PlaybackStatus``. Unknown values map to
    ``Stopped`` so a malformed status never makes MPRIS lie."""
    return {"play": "Playing", "pause": "Paused", "stop": "Stopped"}.get(state, "Stopped")


def loop_status_from(repeat: bool, single: bool) -> str:
    """MPD's two-flag (repeat, single) -> MPRIS ``LoopStatus``.
    ``single`` without ``repeat`` doesn't loop, hence ``None``."""
    if repeat and single:
        return "Track"
    if repeat:
        return "Playlist"
    return "None"


def parse_loop_flags(status: dict) -> tuple[bool, bool]:
    """Extract MPD ``(repeat, single)`` flags as booleans. Bridge keeps
    ``repeat`` separately for ``CanGoNext`` (repeat ⇒ playlist wraps)."""
    return (
        status.get("repeat", "0") == "1",
        status.get("single", "0") == "1",
    )


def parse_shuffle(status: dict) -> bool:
    return bool(status.get("random", "0") == "1")


def parse_volume(status: dict) -> float | None:
    """Return MPRIS-style volume (0.0..1.0) from MPD status, or None
    when MPD reports -1 (audio backend can't report — leave as-is)."""
    try:
        v = int(status.get("volume", -1))
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v >= 0 else None


def parse_elapsed(status: dict) -> float:
    try:
        return float(status.get("elapsed", 0.0))
    except (TypeError, ValueError):
        return 0.0


def song_url(
    song: dict,
    music_dir: Path | None = None,
    url_handlers: Iterable[str] = DEFAULT_URL_HANDLERS,
) -> str:
    """Resolve MPD's ``file`` field into a MPRIS-facing URI. Returns ``""``
    when no file is set. Schemes in ``url_handlers`` are passed through
    untouched; relative paths get absolutised against ``music_dir``
    (when set) and turned into ``file://`` URIs."""
    file_uri = first(song.get("file", "")) if song else ""
    if not file_uri:
        return ""
    if any(file_uri.startswith(h) for h in url_handlers) or not music_dir:
        return file_uri
    return (music_dir / file_uri).as_uri()


def to_mpd_uri(
    url: str,
    music_dir: Path | None = None,
    url_handlers: Iterable[str] = DEFAULT_URL_HANDLERS,
) -> str:
    """Inverse of ``song_url``: a MPRIS-facing URI -> something MPD's
    ``add``/``addid`` accepts. ``file:///srv/music/a/b.flac`` with
    ``music_dir=/srv/music`` gives the library-relative ``a/b.flac``;
    ``http://stream`` passes through when MPD handles the scheme.
    Returns ``""`` for what MPD can't play (a file outside the library,
    an unhandled scheme)."""
    if url.startswith("file://"):
        if not music_dir:
            return ""
        path = Path(unquote(urlsplit(url).path))
        try:
            return path.relative_to(music_dir).as_posix()
        except ValueError:
            return ""
    if any(url.startswith(h) for h in url_handlers if h != "file://"):
        return url
    return ""


# --- currentsong() -> Metadata --------------------------------------------


def mpd_to_mpris(
    song: dict,
    music_dir: Path | None = None,
    url_handlers: Iterable[str] = DEFAULT_URL_HANDLERS,
) -> dict[str, Variant]:
    """Translate ``song`` (the dict returned by ``MPD.currentsong()``)
    to an MPRIS Metadata dict with ``Variant``-wrapped values.

    ``music_dir`` is the local filesystem path used to absolutise
    relative MPD paths into a proper ``xesam:url``. ``url_handlers``
    lists URI schemes MPD already returns as-is so we don't prepend
    ``music_dir`` to them.
    """
    out: dict[str, Variant] = {}
    if not song:
        return out

    def setv(key: str, sig: str, val: object) -> None:
        out[key] = Variant(sig, val)

    # --- string tags --------------------------------------------------
    for mpd_key, mpris_key in (("title", "xesam:title"),
                               ("album", "xesam:album")):
        if mpd_key in song:
            setv(mpris_key, "s", first(song[mpd_key]))

    # --- list-valued tags --------------------------------------------
    for mpd_key, mpris_key in (("artist", "xesam:artist"),
                               ("albumartist", "xesam:albumArtist"),
                               ("composer", "xesam:composer"),
                               ("genre", "xesam:genre")):
        if mpd_key in song:
            setv(mpris_key, "as", _to_list(song[mpd_key]))

    # CDDA / CUE tracks frequently carry only ``albumartist``. MPRIS
    # clients overwhelmingly read ``xesam:artist`` for the track-row
    # artist column, so mirror albumArtist into artist when artist is
    # missing.
    if "xesam:artist" not in out and "xesam:albumArtist" in out:
        out["xesam:artist"] = out["xesam:albumArtist"]

    # --- identifiers --------------------------------------------------
    if "id" in song:
        setv("mpris:trackid", "o", track_id(song["id"]))

    # --- duration -----------------------------------------------------
    # MPD has both ``time`` (seconds, deprecated) and ``duration``
    # (seconds, float, MPD >= 0.20). Prefer ``duration`` when present.
    duration_s: float | None = None
    if "duration" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(first(song["duration"]))
    elif "time" in song:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(first(song["time"]))
    if duration_s is not None and duration_s > 0:
        setv("mpris:length", "x", int(duration_s * 1_000_000))

    # --- dates --------------------------------------------------------
    if "date" in song:
        date = first(song["date"])
        # MPRIS expects ISO-8601-ish; mpDris2 historically just kept the
        # leading year. Anything more elaborate is below the noise floor
        # for MPRIS clients.
        if len(date) >= 4 and date[:4].isdigit():
            setv("xesam:contentCreated", "s", date[:4])

    # --- track / disc numbers ----------------------------------------
    if "track" in song:
        n = _parse_leading_int(first(song["track"]))
        if n is not None:
            # Ensure the integer fits in a signed int32 — MPRIS uses ``i``.
            if n & 0x80000000:
                n -= 0x100000000
            setv("xesam:trackNumber", "i", n)
    if "disc" in song:
        n = _parse_leading_int(first(song["disc"]))
        if n is not None:
            setv("xesam:discNumber", "i", n)

    # --- stream-style metadata fallback -------------------------------
    # Some streams (web radio) only set ``name`` and ``title``: derive
    # an album/title from ``name`` so MPRIS clients have something to
    # display.
    if "name" in song:
        if "xesam:title" not in out:
            setv("xesam:title", "s", first(song["name"]))
        elif "xesam:album" not in out:
            setv("xesam:album", "s", first(song["name"]))

    # --- url ----------------------------------------------------------
    url = song_url(song, music_dir, url_handlers)
    if url:
        setv("xesam:url", "s", url)

    return out
