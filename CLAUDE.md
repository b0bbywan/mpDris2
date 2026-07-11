# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mpd2mpris (formerly mpDris2) is a Python 3 asyncio daemon that provides MPRIS 2 (Media Player Remote Interfacing Specification) D-Bus interface support for MPD (Music Player Daemon). It monitors a local or remote MPD server and exposes it as an MPRIS2-compliant media player on the session D-Bus, using `python-mpd2` for the MPD protocol and `dbus-fast` for the MPRIS interface. No threads, no GLib.

## Build System

`pyproject.toml` (setuptools backend) is the build entry point; `mpd2mpris/__init__.py` is the version source of truth.

```bash
# Dev install + tooling
pip install -e '.[dev,cover]'

# Lint, type-check, tests
make lint
make test

# Build the Debian package (needs a Debian toolchain — use the
# debian:trixie container that CI uses for parity).
make deb

# Sync the .deb changelog with __init__.py before tagging a release
make sync-deb
```

For Nix users, `shell.nix` provides a development shell.

## Source Structure

Flat package at `mpd2mpris/`:

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `__version__` (parsed by `scripts/version.py` and `pyproject.toml`) |
| `__main__.py` | `python -m mpd2mpris` entry point |
| `cli.py` | argparse, INI config loading, `asyncio.run(run(cfg, args))` |
| `bridge.py` | `MpdMprisBridge` — MPD connect/reconnect, D-Bus export, MPRIS callbacks, idle-driven `refresh()` (incl. the queue → TrackList sync: `playlistinfo` re-fetched only when status's `playlist` version moves, diffed into TrackAdded/TrackRemoved or one TrackListReplaced) |
| `mpd_client.py` | `mpd.asyncio.MPDClient` wrapper: connect-with-backoff + capability probe |
| `mpris.py` | `dbus_fast.ServiceInterface` classes: `MediaPlayer2` (root) + `MediaPlayer2Player` + `MediaPlayer2TrackList` (the MPD queue; track ids are `/org/mpris/MediaPlayer2/Track/<songid>`) |
| `translate.py` | Pure MPD song dict → MPRIS Metadata dict (`Variant`-wrapped); also `split_title` (shared ``Artist - Track`` ICY-title parser used by the web-radio resolvers). |
| `cover.py` | 7-step async cover pipeline (MPD readpicture → filesystem regex → MPD albumart → CUE/cdda fallback → remote cover URL → web-radio station favicon → myMPD WebradioDB). Steps 1–4 yield local/embedded covers (bytes → tempfile, or a `file://`). Step 5 delegates to `musicbrainz.py`/`itunes.py`/`deezer.py` for a remote image **URL** used as `mpris:artUrl` unchanged — the image is never downloaded or cached (lighter, and we link rather than re-host), cached per (artist, album); MusicBrainz/CAA, iTunes and Deezer are selected by name via `[Cover] sources` (an ordered list — the order is the lookup priority, a source not listed is off; unset = none). For web radio (title only) each source resolves the album within its own catalogue via `cover_for_track`, so a wrong guess from one doesn't poison the others. Steps 6 (station favicon URL, `radiobrowser.py`) and 7 (myMPD WebradioDB cover, `mympd.py`) cover http(s):// streams; both are selected by name via `[Cover] stream_sources` (same ordered-list rule as `sources` — the order is the priority, unlisted = off), the `mympd` source also needing `[Cover] mympd_uri`. The favicon is HEAD-checked (`_http.is_image`) and skipped if it 404s or isn't an image (some entries point at the station homepage). |
| `musicbrainz.py` | Optional MusicBrainz / Cover Art Archive lookups (isolates the `musicbrainzngs` dep): `cover_url(artist, album)` (release-group front-cover URL via the CAA image list — no image download) and `cover_for_track(artist, track)` (recording search + artist/title validation → album → CAA cover, for web radio). No-op when the dep is absent. Opt-in via `[Cover] musicbrainz`. |
| `itunes.py` / `deezer.py` | No-auth, stdlib-only cover-art sources, each exposing `cover_url(artist, album)` (tagged) and `cover_for_track(artist, track)` (web radio — the cover comes straight from the track/song search). Enabled by listing them in `[Cover] sources` (priority order) — CAA coverage is sparse for some content. |
| `radiobrowser.py` | No-auth, stdlib-only http(s):// web-radio source (cover.py step 6, `radiobrowser` in `[Cover] stream_sources`): `station_icon(stream_url)` returns the station's favicon **URL** used as `mpris:artUrl` — the image isn't downloaded. |
| `mympd.py` | No-auth, stdlib-only http(s):// web-radio source (cover.py step 7, `mympd` in `[Cover] stream_sources` + `[Cover] mympd_uri`): `cover_url(base_url, stream_url)` POSTs `MYMPD_API_WEBRADIODB_RADIO_GET_BY_URI` to a myMPD instance and returns the WebradioDB `Image` **URL** used as `mpris:artUrl` — the image isn't downloaded. No-op when `mympd_uri` is unset. |
| `_http.py` | Shared stdlib helpers for the no-auth fallbacks (`deezer`/`itunes`/`mympd`/`radiobrowser`): `get`/`post` (urllib + User-Agent + timeout) and the loose `artist_matches`. Errors propagate so cover.py skips caching a transient failure. `musicbrainz.py` uses `musicbrainzngs` + its own fuzzy matcher instead. |

Helper scripts: `scripts/version.py` parses `__init__.py` and produces both PEP 440 and Debian-sortable forms. Used by `make sync-deb` and `make check-tag`.

## Runtime Dependencies

- Python 3.11+
- `python-mpd2 >= 3.1`
- `dbus-fast >= 2.0`

Optional (`pip install '.[cover]'`, Debian `python3-musicbrainzngs` Recommends) — enables the cover.py step-5 MusicBrainz lookup: `musicbrainzngs >= 0.7` (recording/release search). Artist/title validation is stdlib (`difflib`-based token ratios), needing no extra dependency. The iTunes/Deezer fallbacks also need no extra deps (stdlib).

Dev: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `build`.

## Configuration

User config at `~/.config/mpd2mpris/mpd2mpris.conf` (INI), falling back to `/etc/mpd2mpris/mpd2mpris.conf`, then the legacy `~/.config/mpDris2/mpDris2.conf` and `/etc/mpDris2/mpDris2.conf` (pre-rename paths, still honoured). Example shipped at `/usr/share/doc/mpd2mpris/mpd2mpris.conf`.

Sections in current use:
- `[Connection]` — `host`, `port`, `password`
- `[Library]` — `music_dir`, `cover_regex`
- `[Cover]` — `sources` (ordered list of `musicbrainz`/`itunes`/`deezer`; unset = none): step-5 remote cover-URL sources, the order being the lookup priority; `stream_sources` (ordered list of `radiobrowser`/`mympd`; unset = none): web-radio stream cover sources (steps 6-7), same rule; `mympd_uri` (str, unset = off): myMPD base URL the `mympd` source needs
- `[Bling]` — `cdprev` (bool)

CLI overrides config: `-H`/`--host`, `-p`/`--port`, `--music-dir`, `--config`, `--use-journal`, `--no-reconnect`, `-v`/`--verbose`.

## Packaging

- **Debian**: `debian/` uses `pybuild-plugin-pyproject` (no autotools). Data files (`data/*.{service,conf}`) are listed in `debian/mpd2mpris.install`.
- **Systemd**: user unit `data/user/mpd2mpris.service` (`Type=dbus`, `BusName=org.mpris.MediaPlayer2.mpd`, `Restart=on-failure`, `ConditionUser=!root` / `ConditionUser=!@system`). Not auto-enabled on install — D-Bus activation kicks it in on the first MPRIS call.
- **D-Bus activation**: `data/dbus-1/org.mpris.MediaPlayer2.mpd.service` → `/usr/share/dbus-1/services/`.
- **CI**: `.github/workflows/build.yml` runs lint + tests on every PR, builds the `.deb` in a `debian:trixie` container on tags, creates a GitHub release, and dispatches to the private `b0bbywan/odio-apt-repo` for APT-repo rebuild.

## Notes

- Media keys: the original GNOME `org.gnome.SettingsDaemon.MediaKeys` grab was dropped during the asyncio rewrite. Modern desktops (GNOME ≥ 3.6, KDE, Sway/Hyprland with `playerctld`) route media keys through MPRIS2 directly.
- The `[Bling] mmkeys` config key is no longer honoured (the GNOME MediaKeys grab was dropped in the asyncio rewrite). `[Bling] cdprev` is still honoured by `bridge.py`.
- Desktop notifications were dropped: the daemon no longer ships `notify.py` or a `[Notify]` section. Modern desktops raise their own bubbles off the exported MPRIS metadata, so a second libnotify layer was redundant.
- The legacy `src/mpDris2.in.py` + autotools build was removed in the same refactor; see `docs/refactor-asyncio-dbus-fast.md` for the migration plan.
