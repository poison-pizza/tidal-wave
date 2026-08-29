#!/usr/bin/env python3
"""
Local usage: python bpm_handler.py
"""

import json
import os
import re
import sys
import time
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
RATE_LIMIT_DELAY = 0.1      # seconds between api calls, raise if you hit rate limits

# AUTH

def save_session(session: tidalapi.Session) -> None:
    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
    }
    TOKEN_FILE.write_text(json.dumps(data))

def load_session(session: tidalapi.Session, data: dict) -> bool:
    try:
        from datetime import datetime
        expiry = (
            datetime.fromisoformat(data["expiry_time"])
            if data.get("expiry_time") else None
        )
        session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data["refresh_token"],
            expiry,
        )
        return session.check_login()
    except Exception:
        return False

def get_session() -> tidalapi.Session:
    session = tidalapi.Session()

    # CI: token comes from TIDAL_SESSION_JSON secret
    ci_token = os.getenv("TIDAL_SESSION_JSON")
    if ci_token:
        try:
            date = json.loads(ci_token)
            if load_session(session, data):
                print("Loaded session from TIDAL_SESSION_JSON")
                save_session(session)
                return session
        except Exception as e:
            sys.exit(f"Failed to load TIDAL_SESSION_JSON: {e}")

    # local: try saved token file
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            if load_session(session, data):
                print("Restored saved session")
                save_session(session) # refresh token may have rotated
                return session
        except Exception:
            pass

    # local fallback: interactive oauth
    if os.getenv("CI"):
        sys.exit(
            "CI mode: no valid TIDAL_SESSION_JSON found. "
            "Run locally first to generate a token, then add it as a secret."
        )
    print("No saved session found, starting oauth login...")
    print("Approve the following link\n")
    session.login_oauth_simple()
    save_session(session)
    print("Logged in and session saved to tidal_token.json")
    return session

# BPM HELPERS

def get_bpm(track: tidalapi.Track, session: tidalapi.Session) -> int | None:
    # approach 1: named attribute (present in some tidalapi builds)
    bpm = getattr(track, "bpm", None)
    if bpm is not None and int(bpm) > 0:
        return int(bpm)

    # approach 2: raw api call
    try:
        raw = session.request.request("GET", f"tracks/{track.id}").json()
        bpm - raw.get("bpm")
        if bpm and int(bpm) > 0:
            return int(bpm)
    except Exception:
        pass
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
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s, re.IGNORECASE):
        return "playlist", session.playlist(s)

    raise ValueError(
        f"Couldn't parse '{s}' as a Tidal album/playlist url or id."
    )

# PLAYLIST OPERATIONS

def get_existing_ids(playlist: tidalapi.Playlist) -> set[int]:
    return {t.id for t in playlist.tracks()}


def sort_playlist_by_bpm(
        playlist: tidalapi.UserPlaylist,
        session: tidalapi.Session,
) -> None:
    print(f"\n Sorting {playlist.name} by BPM...")
    tracks = playlist.tracks()

    if not tracks:
        print("playlist is empty, nothing to sort")
        return

    track_bpm: list[tuple[int, int, str]] = []
    for t in tracks:
        bpm = get_bpm(t, session)
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
                print(f"Couldn't remove track {tid}: {e}")
            time.sleep(RATE_LIMIT_DELAY)

    sorted_ids = [tid for _, tid, _ in track_bpm]
    playlist.add(sorted_ids)

    print(f"Sorted {len(sorted_ids)} tracks.")
    for bpm_val, name in track_bpm:
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

# MAIN

def main() -> None:
    CI = bool(os.getenv("CI"))

    print("=" * 20)
    print("Tidal BPM sorter" + ("[CI mode]") if CI else "")
    print("=" * 20)

    BPM_PLAYLISTS = get_bpm_playlists()
    placeholders = [pid for pid in BPM_PLAYLISTS.values() if pid.startswith("YOUR-")]
    if placeholders:
        sys.exit(
            "Missing playlist UUIDs, either hardcode or set env vars"
        )

    session = get_session()
    print()

    # load bpm target playlists
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

    # categorize tracks
    to_add: dict[tuple[int, int], list[tuple[int, str, int]]] = {k: [] for k in BPM_PLAYLISTS}
    skipped_no_bpm: list[str] = []
    skipped_out_of_range: list[str] = []
    skipped_duplicate: list[str] = []

    for track in tracks:
        bpm = get_bpm(track, session)
        time.sleep(RATE_LIMIT_DELAY)

        if bpm is None:
            skipped_no_bpm.append(track.name)
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

    # add to playlists
    print()
    modified: list[tuple[int, int]] = []
    for key, items in to_add.items():
        if not items:
            continue
        pl = bpm_playlists[key]
        pl.add([tid for tid, _, _ in items])
        print(f"Added {len(items)} track(s) to '{pl.name}'")
        modified.append(key)

    # summary
    print(f"\nSummary")
    print(f"Added: {sum(len(v) for v in to_add.values())}")
    print(f"Already present: {len(skipped_duplicate)}")
    print(f"Out of range: {len(skipped_out_of_range)}")
    print(f"No BPM data: {len(skipped_no_bpm)}")
    if skipped_no_bpm:
        print(f"\n Tracks with no BPM data:")
        for name in skipped_no_bpm:
            print(f" - {name}")

    # sort
    if CI:
        sort_choice = os.getenv("SORT_CHOICE", "none")
        keys_to_sort = resolve_sort_choice(sort_choice, bpm_playlists, modified)
    else:
        print()
        print("Sort BPM playlists by BPM?")
        for i, ((lo, hi), pl) in enumerate(bpm_playlists.items(), start=1):
            tag = " <- updated this run" if (lo, hi) in modified else ""
            print(f" {i}) {pl.name}{tag}")
        print(" updated) Only updated playlists")
        print(" all) All playlists")
        print(" n) Skip")
        choice = input("Choice: ").strip()
        keys_to_sort = resolve_sort_choice(choice, bpm_playlists, modified)

    for key in keys_to_sort:
        sort_playlist_by_bpm(bpm_playlists[key], session)

    print("\nDone")



if __name__ == "__main__":
    main()