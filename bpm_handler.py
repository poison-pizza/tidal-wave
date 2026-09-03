#!/usr/bin/env python3
"""
Local usage: python bpm_handler.py
"""

from datetime import datetime
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

try:
    import tidalapi
except ImportError:
    sys.exit("Missing dependency: pip install tidalapi")

# CONFIG


def get_bpm_playlists() -> dict[tuple[int, int], str]:
    return {
        (110, 119): os.getenv("BPM_PLAYLIST_110", "YOUR_PLAYLIST_UUID_110"),
        (120, 129): os.getenv("BPM_PLAYLIST_120", "YOUR_PLAYLIST_UUID_120"),
        (130, 139): os.getenv("BPM_PLAYLIST_130", "YOUR_PLAYLIST_UUID_130"),
    }


TOKEN_FILE = Path("tidal_token.json")
OVERRIDES_FILE = Path("bpm_overrides.json")
RATE_LIMIT_DELAY = 0.1  # seconds between api calls, raise if you hit rate limits

# AUTH


def save_session(session: tidalapi.Session) -> None:
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "token_type": session.token_type,
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expiry_time": (
                    session.expiry_time.isoformat() if session.expiry_time else None
                ),
            }
        )
    )


def load_session(session: tidalapi.Session, token_data: dict) -> bool:
    expiry = (
        datetime.fromisoformat(token_data["expiry_time"])
        if token_data.get("expiry_time")
        else None
    )
    session.load_oauth_session(
        token_data["token_type"],
        token_data["access_token"],
        token_data["refresh_token"],
        expiry,
    )
    return session.check_login()


def try_load_token(session: tidalapi.Session, token_data: dict, label: str) -> bool:
    try:
        if load_session(session, token_data):
            return True
        print(f"check_login returned False for {label}, attempting token refresh...")
        try:
            session.token_refresh(token_data["refresh_token"])
            if session.check_login():
                print("Token refreshed successfully")
                return True
        except Exception as refresh_err:
            print(f"Token refresh also failed: {refresh_err}")
        return False
    except Exception:
        print(f"Failed to load {label}. Full traceback:")
        traceback.print_exc()
        return False


def get_session() -> tidalapi.Session:
    session = tidalapi.Session()

    # CI: token comes from TIDAL_SESSION_JSON secret
    ci_token = os.getenv("TIDAL_SESSION_JSON")
    if ci_token:
        try:
            token_data = json.loads(ci_token)
        except json.JSONDecodeError as e:
            sys.exit(
                f"TIDAL_SESSION_JSON is not valid JSON: {e}\n"
                "Rerun locally and copy the contents of tidal_token.json and update secret."
            )
        if try_load_token(session, token_data, "TIDAL_SESSION_JSON"):
            print("Loaded session from TIDAL_SESSION_JSON")
            save_session(session)
            return session
        else:
            sys.exit(
                "Could not authenticate. Rerun locally and update TIDAL_SESSION_JSON with new tidal_token.json"
            )
    # local: try saved token file
    if TOKEN_FILE.exists():
        try:
            token_data = json.loads(TOKEN_FILE.read_text())
            if try_load_token(session, token_data, TOKEN_FILE.name):
                print("Restored saved session")
                save_session(session)
                return session
        except Exception:
            print(f"Could not read {TOKEN_FILE}, will re-authenticate")

    # local fallback: interactive oauth
    if os.getenv("CI"):
        sys.exit(
            "CI mode: no TIDAL_SESSION_JSON found, run locally to generate token and add as secret."
        )

    print("No saved session found, starting oauth login...")
    try:
        session.login_oauth()  # opens browser automatically
    except Exception:
        # Fallback if browser can't open (e.g. headless environment)
        session.login_oauth_simple()
    save_session(session)
    print("Logged in and session daved to tidal_token.json")
    return session


# ── BPM OVERRIDES ─────────────────────────────────────────────────────────────


def load_overrides() -> dict[str, dict]:
    """
    Load manual BPM overrides from bpm_overrides.json.
    Keys are track IDs as strings; values are dicts with at least {"bpm": int}.
    """
    if OVERRIDES_FILE.exists():
        try:
            return json.loads(OVERRIDES_FILE.read_text())
        except Exception as e:
            print(f"Warning: could not read {OVERRIDES_FILE}: {e}")
    return {}


def save_overrides(overrides: dict[str, dict]) -> None:
    OVERRIDES_FILE.write_text(json.dumps(overrides, indent=2))


def parse_manual_bpms(raw: str) -> dict[str, dict]:
    """
    Parse the MANUAL_BPMS env var / workflow input.
    Format: "trackid1:120,trackid2:130"
    Returns a partial overrides dict suitable for merging.
    """
    result: dict[str, dict] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            print(
                f"  Warning: ignoring malformed MANUAL_BPMS entry '{pair}' (expected trackid:bpm)"
            )
            continue
        tid, bpm_str = pair.split(":", 1)
        try:
            result[tid.strip()] = {
                "bpm": int(bpm_str.strip()),
                "title": "CI manual input",
            }
        except ValueError:
            print(f"  Warning: ignoring non-integer BPM in '{pair}'")
    return result


# BPM HELPERS


def get_bpm(
    track: tidalapi.Track,
    session: tidalapi.Session,
    overrides: dict[str, dict] | None = None,
) -> int | None:
    # 1. Overrides take priority
    if overrides and str(track.id) in overrides:
        return int(overrides[str(track.id)]["bpm"])

    # 2. tidalapi attribute (present on pre-loaded track objects)
    bpm = getattr(track, "bpm", None)
    if bpm is not None and int(bpm) > 0:
        return int(bpm)

    country = session.country_code or "US"

    # 3. Raw API call using direct track ID
    try:
        raw = session.request.request(
            "GET", 
            f"tracks/{track.id}",
            params={"countryCode": country}
        ).json()
        bpm = raw.get("bpm")
        if bpm and int(bpm) > 0:
            return int(bpm)
    except Exception:
        # 4. Fallback: If track ID is delisted/404, search for active track version
        try:
            artist_name = track.artist.name if hasattr(track, "artist") and track.artist else ""
            query = f"{track.name} {artist_name}".strip()
            
            search_results = session.search(query, models=[tidalapi.Track], limit=1)
            found_tracks = search_results.get("tracks", []) if isinstance(search_results, dict) else getattr(search_results, "tracks", [])

            if found_tracks:
                active_track = found_tracks[0]
                
                # Check active track attribute
                bpm = getattr(active_track, "bpm", None)
                if bpm and int(bpm) > 0:
                    return int(bpm)

                # Query active track endpoint
                raw = session.request.request(
                    "GET",
                    f"tracks/{active_track.id}",
                    params={"countryCode": country}
                ).json()
                bpm = raw.get("bpm")
                if bpm and int(bpm) > 0:
                    return int(bpm)
        except Exception as fallback_err:
            print(f"[debug] search fallback failed for {track.name}: {fallback_err}")

    return None


def bpm_range_for(bpm: int, playlists: dict) -> tuple[int, int] | None:
    for lo, hi in playlists:
        if lo <= bpm <= hi:
            return (lo, hi)
    return None


# INPUT PARSING


def parse_source(raw: str, session: tidalapi.Session):
    s = raw.strip().rstrip("/")

    album_match = re.search(r"/album/(\d+)", s)
    playlist_match = re.search(r"/playlist/([0-9a-f-]{36})", s, re.IGNORECASE)

    if album_match:
        return "album", session.album(int(album_match.group(1)))
    if playlist_match:
        return "playlist", session.playlist(playlist_match.group(1))
    if re.fullmatch(r"\d+", s):
        return "album", session.album(int(s))
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        s,
        re.IGNORECASE,
    ):
        return "playlist", session.playlist(s)

    raise ValueError(f"Couldn't parse '{s}' as a Tidal album/playlist url or id.")


# PLAYLIST OPERATIONS


def get_existing_ids(playlist: tidalapi.Playlist) -> set[int]:
    return {t.id for t in playlist.tracks()}


def sort_playlist_by_bpm(
    playlist: tidalapi.UserPlaylist,
    session: tidalapi.Session,
    overrides: dict[str, dict] | None = None,
) -> None:
    print(f"\n Sorting {playlist.name} by BPM...")
    playlist = session.playlist(playlist.id)
    tracks = playlist.tracks()

    for _ in range(5):
        time.sleep(1)
        playlist = session.playlist(playlist.id)
        fresh = playlist.tracks()
        if len(fresh) == len(tracks):
            break
        print(f"Playlist count changed, ({len(tracks)} -> {len(fresh)})")

    if not tracks:
        print("playlist is empty, nothing to sort")
        return

    track_bpm: list[tuple[int, int, str]] = []
    for t in tracks:
        bpm = get_bpm(t, session, overrides)
        track_bpm.append((bpm if bpm else 9999, t.id, t.name))
        time.sleep(RATE_LIMIT_DELAY)

    track_bpm.sort()

    print(f"Clearing {len(tracks)} tracks...")
    removed_ids: set[int] = set()
    for _, tid, _ in track_bpm:
        if tid not in removed_ids:
            try:
                playlist.remove_by_id(tid)
                removed_ids.add(tid)
            except Exception as e:
                if "412" in str(e):
                    # ETag went stale mid-loop — reload and retry once
                    playlist = session.playlist(playlist.id)
                    try:
                        playlist.remove_by_id(tid)
                        removed_ids.add(tid)
                    except Exception as e2:
                        print(f"    Warning: couldn't remove track {tid}: {e2}")
                else:
                    print(f"Couldn't remove track {tid}: {e}")
            time.sleep(RATE_LIMIT_DELAY)

    # Reload once more — ETag changes again after the removes
    playlist = session.playlist(playlist.id)
    playlist.add([tid for _, tid, _ in track_bpm])

    print(f"Sorted {len(track_bpm)} tracks.")
    for bpm_val, _, name in track_bpm:
        display = f"{bpm_val} BPM" if bpm_val != 9999 else "no BPM"
        print(f"{display:>10} {name}")


def resolve_sort_choice(
    choice: str,
    bpm_playlists: dict[tuple[int, int], tidalapi.UserPlaylist],
    modified: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    parse a sort choice string into a list of playlist keys.

    accepted values:
        none/'' = skip
        all = all 3 bpm playlists
        updated = only playlists touched this run
        1/2/3 = by index
    """
    c = choice.strip().lower()
    all_keys = list(bpm_playlists.keys())

    if c in ("none", ""):
        return []
    if c == "all":
        return all_keys
    if c == "updated":
        return list(modified)

    keys = []
    for part in re.split(r"[,\s]+", c):
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(all_keys):
                keys.append(all_keys[idx])
    return keys


def add_tracks_to_playlists(
    tracks_to_add: dict[tuple[int, int], list[tuple[int, str, int]]],
    bpm_playlists: dict[tuple[int, int], tidalapi.UserPlaylist],
    modified: list[tuple[int, int]],
) -> None:
    """Batch-add queued tracks to their target playlists."""
    for key, items in tracks_to_add.items():
        if not items:
            continue
        pl = bpm_playlists[key]
        current_ids = get_existing_ids(pl)
        new_items = [(tid, name, bpm) for tid, name, bpm in items if tid not in current_ids]
        dupes = len(items) - len(new_items)
        if dupes:
            print(f"Skipped {dupes} duplicate(s) already in '{pl.name}'")
        if not new_items:
            continue
        pl.add([tid for tid, _, _ in new_items])
        print(f"✓ Added {len(items)} track(s) to '{pl.name}'")
        if key not in modified:
            modified.append(key)


# MAIN


def main() -> None:
    CI = bool(os.getenv("CI"))

    print("=" * 20)
    print("Tidal BPM sorter" + ("[CI mode]") if CI else "")
    print("=" * 20)

    BPM_PLAYLISTS = get_bpm_playlists()
    placeholders = [pid for pid in BPM_PLAYLISTS.values() if pid.startswith("YOUR_")]
    if placeholders:
        sys.exit("Missing playlist UUIDs, either hardcode or set env vars")

    session = get_session()
    print()

    # Load overrides (committed file + any CI one-offs)
    overrides = load_overrides()
    if overrides:
        print(f"Loaded {len(overrides)} BPM override(s) from {OVERRIDES_FILE}\n")

    if CI:
        manual_raw = os.getenv("MANUAL_BPMS", "").strip()
        if manual_raw:
            ci_overrides = parse_manual_bpms(manual_raw)
            overrides.update(ci_overrides)
            print(f"  Applied {len(ci_overrides)} MANUAL_BPMS override(s) for this run")

    # Load BPM target playlists
    bpm_playlists: dict[tuple[int, int], tidalapi.UserPlaylist] = {}
    for (lo, hi), pid in BPM_PLAYLISTS.items():
        pl = session.playlist(pid)
        bpm_playlists[(lo, hi)] = pl
        print(f"loaded {pl.name}")
    print()

    # get source url
    if CI:
        source_raw = os.getenv("SOURCE_URL", "").strip()
        if not source_raw:
            sys.exit("CI mode: SOURCE_URL is required.")
    else:
        source_raw = input("Album or playlist URL or ID: ").strip()
        if not source_raw:
            sys.exit("No input provided")

    try:
        kind, source = parse_source(source_raw, session)
    except ValueError as e:
        sys.exit(str(e))

    tracks = source.tracks()
    label = f"album '{source.name}'" if kind == "album" else f"playlist '{source.name}'"
    print(f"Scanning {label} - {len(tracks)} tracks\n")

    # cache existing ids
    print("Fetching existing playlist contents...")
    existing: dict[tuple[int, int], set[int]] = {
        key: get_existing_ids(pl) for key, pl in bpm_playlists.items()
    }
    print()

    # Scan tracks
    to_add: dict[tuple[int, int], list[tuple[int, str, int]]] = {
        k: [] for k in BPM_PLAYLISTS
    }
    no_bpm_tracks: list[tidalapi.Track] = []  # full objects for local manual input
    skipped_out_of_range: list[str] = []
    skipped_duplicate: list[str] = []

    for track in tracks:
        bpm = get_bpm(track, session, overrides)
        time.sleep(RATE_LIMIT_DELAY)

        if bpm is None:
            no_bpm_tracks.append(track)
            continue

        key = bpm_range_for(bpm, BPM_PLAYLISTS)
        if key is None:
            skipped_out_of_range.append(f"{track.name} ({bpm} BPM)")
            continue

        if track.id in existing[key]:
            skipped_duplicate.append(f"{track.name} ({bpm} BPM)")
            continue

        to_add[key].append((track.id, track.name, bpm))
        existing[key].add(track.id)
        lo, hi = key
        print(f" + {bpm:>3} BPM {track.name} -> {lo}-{hi}")

    # Add first-pass tracks
    print()
    modified: list[tuple[int, int]] = []
    add_tracks_to_playlists(to_add, bpm_playlists, modified)

    # summary
    print(f"\nSummary")
    print(f"Added: {sum(len(v) for v in to_add.values())}")
    print(f"Already present: {len(skipped_duplicate)}")
    print(f"Out of range: {len(skipped_out_of_range)}")
    print(f"No BPM data: {len(no_bpm_tracks)}")
    if no_bpm_tracks:
        for t in no_bpm_tracks:
            override_note = " (override exists)" if str(t.id) in overrides else ""
            print(f"    - {t.name}{override_note}")

    # ── Manual BPM entry (local mode only) ───────────────────────────────────
    if not CI and no_bpm_tracks:
        unentered = [t for t in no_bpm_tracks if str(t.id) not in overrides]
        if unentered:
            print(f"\n{len(unentered)} track(s) have no BPM data anywhere.")
            fill = input("Enter BPMs manually now? (y/n): ").strip().lower()
            if fill == "y":
                override_to_add: dict[tuple[int, int], list[tuple[int, str, int]]] = {
                    k: [] for k in BPM_PLAYLISTS
                }
                newly_saved = 0

                for track in unentered:
                    raw_bpm = input(
                        f"  BPM for '{track.name}' - {track.artist} (Enter to skip): "
                    ).strip()
                    if not raw_bpm:
                        continue
                    try:
                        bpm = int(raw_bpm)
                    except ValueError:
                        print("    Not a number, skipping.")
                        continue

                    # Save to overrides
                    overrides[str(track.id)] = {
                        "bpm": bpm,
                        "title": track.name,
                        "artist": (
                            track.artist.name
                            if hasattr(track, "artist") and track.artist
                            else ""
                        ),
                    }
                    newly_saved += 1

                    # Queue for adding to playlist
                    key = bpm_range_for(bpm, BPM_PLAYLISTS)
                    if key is None:
                        print(
                            f"    {bpm} BPM — out of range, won't be added to a playlist"
                        )
                        continue
                    if track.id in existing[key]:
                        print(f"    Already in playlist")
                        continue
                    override_to_add[key].append((track.id, track.name, bpm))
                    existing[key].add(track.id)
                    lo, hi = key
                    print(f"    → queued for {lo}–{hi} playlist")

                if newly_saved:
                    save_overrides(overrides)
                    print(f"\n✓ Saved {newly_saved} override(s) to {OVERRIDES_FILE}")
                    print("  Commit this file to your repo so CI runs pick it up.\n")
                    add_tracks_to_playlists(override_to_add, bpm_playlists, modified)

    # ── Sort ─────────────────────────────────────────────────────────────────
    if CI:
        sort_choice = os.getenv("SORT_CHOICE", "none")
        keys_to_sort = resolve_sort_choice(sort_choice, bpm_playlists, modified)
    else:
        print()
        print("Sort BPM playlists by BPM?")
        for i, ((lo, hi), pl) in enumerate(bpm_playlists.items(), start=1):
            tag = " ← updated this run" if (lo, hi) in modified else ""
            print(f"  {i}) {pl.name}{tag}")
        print("  updated) Only updated playlists")
        print("  all)     All playlists")
        print("  n)       Skip")
        keys_to_sort = resolve_sort_choice(
            input("Choice: ").strip(), bpm_playlists, modified
        )

    for key in keys_to_sort:
        sort_playlist_by_bpm(bpm_playlists[key], session, overrides)

    print("\nDone")


if __name__ == "__main__":
    main()
