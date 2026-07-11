"""Pure-function tests for the translate module — no D-Bus, no MPD."""

from __future__ import annotations

from pathlib import Path

import pytest

from mpd2mpris.translate import (
    NO_TRACK,
    artist_matches,
    first,
    loop_status_from,
    mpd_to_mpris,
    parse_elapsed,
    parse_loop_flags,
    parse_shuffle,
    parse_volume,
    playback_status_from,
    song_url,
    songid_from,
    split_title,
    to_mpd_uri,
    track_id,
)


def test_empty_song_returns_empty_dict() -> None:
    assert mpd_to_mpris({}) == {}


def test_basic_tags() -> None:
    m = mpd_to_mpris({
        "title": "Song",
        "album": "Album",
        "artist": "Artist",
        "albumartist": "AA",
        "composer": "C",
        "genre": "Pop",
        "id": "42",
        "track": "3",
        "disc": "2",
        "duration": "245.123",
        "date": "2023-06-15",
        "file": "Artist/Album/03 - Song.mp3",
    }, music_dir=Path("/srv/music"))
    assert m["xesam:title"].value == "Song"
    assert m["xesam:album"].value == "Album"
    assert m["xesam:artist"].value == ["Artist"]
    assert m["xesam:albumArtist"].value == ["AA"]
    assert m["xesam:composer"].value == ["C"]
    assert m["xesam:genre"].value == ["Pop"]
    assert m["mpris:trackid"].value == "/org/mpris/MediaPlayer2/Track/42"
    assert m["mpris:trackid"].signature == "o"
    assert m["xesam:trackNumber"].value == 3
    assert m["xesam:discNumber"].value == 2
    assert m["mpris:length"].value == 245_123_000  # microseconds
    assert m["xesam:contentCreated"].value == "2023"
    # music_dir prepended for relative paths (URL-encoded by as_uri)
    assert m["xesam:url"].value == "file:///srv/music/Artist/Album/03%20-%20Song.mp3"


def test_multi_artist_list_preserved() -> None:
    m = mpd_to_mpris({"artist": ["A", "B", "C"]})
    assert m["xesam:artist"].value == ["A", "B", "C"]
    assert m["xesam:artist"].signature == "as"


def test_artist_backfilled_from_albumartist() -> None:
    # CDDA / CUE tracks frequently expose only ``albumartist``.
    m = mpd_to_mpris({"albumartist": "AA", "title": "T"})
    assert m["xesam:artist"].value == ["AA"]
    assert m["xesam:albumArtist"].value == ["AA"]


def test_artist_not_overwritten_when_present() -> None:
    m = mpd_to_mpris({"artist": "Track Artist", "albumartist": "Album Artist"})
    assert m["xesam:artist"].value == ["Track Artist"]
    assert m["xesam:albumArtist"].value == ["Album Artist"]


def test_track_with_total_only_keeps_leading_int() -> None:
    # "3/12" is a common MPD format meaning track 3 of 12.
    m = mpd_to_mpris({"track": "3/12"})
    assert m["xesam:trackNumber"].value == 3


def test_url_with_scheme_left_untouched() -> None:
    m = mpd_to_mpris(
        {"file": "http://stream.example/live.mp3"},
        music_dir=Path("/srv/music"),
    )
    assert m["xesam:url"].value == "http://stream.example/live.mp3"


def test_stream_name_fills_missing_title() -> None:
    m = mpd_to_mpris({"name": "Radio Example", "file": "http://r/x.mp3"})
    assert m["xesam:title"].value == "Radio Example"


def test_stream_name_fills_album_when_title_present() -> None:
    m = mpd_to_mpris({
        "name": "Radio Example",
        "title": "Song - Artist",
    })
    assert m["xesam:title"].value == "Song - Artist"
    assert m["xesam:album"].value == "Radio Example"


def test_duration_takes_precedence_over_time() -> None:
    # MPD ships both; ``duration`` is the float-precision modern one.
    m = mpd_to_mpris({"time": "180", "duration": "180.456"})
    assert m["mpris:length"].value == 180_456_000


def test_unparseable_track_dropped_silently() -> None:
    m = mpd_to_mpris({"track": "garbage"})
    assert "xesam:trackNumber" not in m


def test_no_duration_no_length_key() -> None:
    m = mpd_to_mpris({"title": "x"})
    assert "mpris:length" not in m


def test_invalid_date_dropped() -> None:
    m = mpd_to_mpris({"date": "n/a"})
    assert "xesam:contentCreated" not in m


def test_first_handles_none() -> None:
    assert first(None) == ""


def test_first_handles_empty_list() -> None:
    assert first([]) == ""


# --- split_title ----------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Mato - 1980 Dub", ("Mato", "1980 Dub")),
    ("  Mato  -  1980 Dub  ", ("Mato", "1980 Dub")),   # trimmed
    ("Artist - Track - With Dash", ("Artist", "Track - With Dash")),  # first sep only
    ("Artist – Track", ("Artist", "Track")),           # en-dash
    ("Artist — Track", ("Artist", "Track")),           # em-dash
    ("Jean-Luc - Mon Titre", ("Jean-Luc", "Mon Titre")),  # hyphenated artist kept intact
    ("Jean-Luc Ponty", None),                          # unspaced hyphen is not a separator
    ("bare station name", None),                       # no separator
    ("Artist - ", None),                               # empty track
    (" - Track", None),                                # empty artist
    ("", None),
])
def test_split_title(title: str, expected: tuple[str, str] | None) -> None:
    assert split_title(title) == expected


# --- artist_matches -------------------------------------------------------

@pytest.mark.parametrize("query,candidate,expected", [
    ("Bob Marley", "Bob Marley", True),
    ("bob marley", "Bob  Marley!", True),              # case + punctuation folded
    ("Bob Marley", "Bob Marley & The Wailers", True),  # query ⊂ candidate
    ("Bob Marley & The Wailers", "Bob Marley", True),  # candidate ⊂ query
    ("Bob Marley", "Peter Tosh", False),
    ("", "Bob Marley", False),                         # empty never matches
    ("Bob Marley", "", False),
])
def test_artist_matches(query: str, candidate: str, expected: bool) -> None:
    assert artist_matches(query, candidate) is expected


# --- playback_status_from -------------------------------------------------

@pytest.mark.parametrize("state,expected", [
    ("play", "Playing"),
    ("pause", "Paused"),
    ("stop", "Stopped"),
    ("", "Stopped"),
    ("garbage", "Stopped"),
])
def test_playback_status_from(state: str, expected: str) -> None:
    assert playback_status_from(state) == expected


# --- loop_status_from -----------------------------------------------------

@pytest.mark.parametrize("repeat,single,expected", [
    (False, False, "None"),
    (True, False, "Playlist"),
    (True, True, "Track"),
    (False, True, "None"),  # single without repeat doesn't loop
])
def test_loop_status_from(repeat: bool, single: bool, expected: str) -> None:
    assert loop_status_from(repeat, single) == expected


# --- parse_loop_flags -----------------------------------------------------

def test_parse_loop_flags_both_off() -> None:
    assert parse_loop_flags({}) == (False, False)


def test_parse_loop_flags_repeat_only() -> None:
    assert parse_loop_flags({"repeat": "1"}) == (True, False)


def test_parse_loop_flags_both_on() -> None:
    assert parse_loop_flags({"repeat": "1", "single": "1"}) == (True, True)


def test_parse_loop_flags_zero_is_false() -> None:
    assert parse_loop_flags({"repeat": "0", "single": "0"}) == (False, False)


# --- parse_shuffle --------------------------------------------------------

def test_parse_shuffle_on() -> None:
    assert parse_shuffle({"random": "1"}) is True


def test_parse_shuffle_off() -> None:
    assert parse_shuffle({"random": "0"}) is False


def test_parse_shuffle_missing() -> None:
    assert parse_shuffle({}) is False


# --- parse_volume ---------------------------------------------------------

def test_parse_volume_valid() -> None:
    assert parse_volume({"volume": "75"}) == 0.75


def test_parse_volume_zero() -> None:
    assert parse_volume({"volume": "0"}) == 0.0


def test_parse_volume_missing_means_no_change() -> None:
    assert parse_volume({}) is None


def test_parse_volume_minus_one_means_unreportable() -> None:
    # MPD returns -1 when the audio backend can't report the level.
    assert parse_volume({"volume": "-1"}) is None


def test_parse_volume_garbage_means_no_change() -> None:
    assert parse_volume({"volume": "loud"}) is None


# --- parse_elapsed --------------------------------------------------------

def test_parse_elapsed_valid() -> None:
    assert parse_elapsed({"elapsed": "12.345"}) == 12.345


def test_parse_elapsed_missing() -> None:
    assert parse_elapsed({}) == 0.0


def test_parse_elapsed_garbage() -> None:
    assert parse_elapsed({"elapsed": "n/a"}) == 0.0


# --- song_url -------------------------------------------------------------

def test_song_url_relative_with_music_dir() -> None:
    song = {"file": "Artist/Album/Song.flac"}
    assert song_url(song, Path("/srv/music"), ["http://"]) == (
        "file:///srv/music/Artist/Album/Song.flac"
    )


def test_song_url_http_passes_through() -> None:
    song = {"file": "http://stream.example/live.mp3"}
    assert song_url(song, Path("/srv/music"), ["http://"]) == (
        "http://stream.example/live.mp3"
    )


def test_song_url_no_music_dir_returns_raw() -> None:
    song = {"file": "Artist/Song.flac"}
    assert song_url(song, None, ["http://"]) == "Artist/Song.flac"


def test_song_url_empty_song() -> None:
    assert song_url({}, Path("/srv/music"), ["http://"]) == ""


def test_song_url_url_encodes_specials() -> None:
    song = {"file": "Artist/Album 01/Song #1.flac"}
    assert song_url(song, Path("/srv/music"), ["http://"]) == (
        "file:///srv/music/Artist/Album%2001/Song%20%231.flac"
    )


# --- track ids --------------------------------------------------------------

def test_track_id_from_songid() -> None:
    assert track_id("42") == "/org/mpris/MediaPlayer2/Track/42"
    assert track_id(7) == "/org/mpris/MediaPlayer2/Track/7"


def test_songid_from_roundtrip() -> None:
    assert songid_from(track_id("42")) == 42


def test_songid_from_no_track_sentinel() -> None:
    assert songid_from(NO_TRACK) is None


def test_songid_from_foreign_path() -> None:
    assert songid_from("/org/mpris/MediaPlayer2/Playlists/1") is None


def test_songid_from_non_numeric() -> None:
    assert songid_from("/org/mpris/MediaPlayer2/Track/abc") is None


# --- to_mpd_uri -------------------------------------------------------------

def test_to_mpd_uri_file_under_music_dir() -> None:
    assert to_mpd_uri(
        "file:///srv/music/Artist/Album/Song.flac", Path("/srv/music"), ["http://"],
    ) == "Artist/Album/Song.flac"


def test_to_mpd_uri_unquotes_specials() -> None:
    assert to_mpd_uri(
        "file:///srv/music/Album%2001/Song%20%231.flac", Path("/srv/music"), ["http://"],
    ) == "Album 01/Song #1.flac"


def test_to_mpd_uri_file_outside_library_rejected() -> None:
    assert to_mpd_uri("file:///home/x/song.mp3", Path("/srv/music"), ["http://"]) == ""


def test_to_mpd_uri_file_without_music_dir_rejected() -> None:
    assert to_mpd_uri("file:///srv/music/a.flac", None, ["http://"]) == ""


def test_to_mpd_uri_http_passes_through() -> None:
    url = "http://stream.example/live.mp3"
    assert to_mpd_uri(url, Path("/srv/music"), ["http://"]) == url


def test_to_mpd_uri_unhandled_scheme_rejected() -> None:
    assert to_mpd_uri("spotify:track:xyz", Path("/srv/music"), ["http://"]) == ""


def test_to_mpd_uri_roundtrips_song_url() -> None:
    song = {"file": "Artist/Album 01/Song #1.flac"}
    url = song_url(song, Path("/srv/music"), ["http://"])
    assert to_mpd_uri(url, Path("/srv/music"), ["http://"]) == song["file"]
