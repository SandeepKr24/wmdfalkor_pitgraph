"""Fetch tyre stints, pit stops, neutralisations and weather from OpenF1.

OpenF1 identifies drivers by car number and races by session_key, so this
module maps both onto the Jolpica IDs already in the graph:
  - session -> race: same season, closest date (within a day)
  - car number -> entry: the driver's 3-letter code (VER, LEC, ...)
"""

from datetime import date
from statistics import mean

from pitgraph.ingest.http import fetch_json

BASE_URL = "https://api.openf1.org/v1"
REQUEST_DELAY_S = 2.1  # free tier allows 30 requests/minute
MAX_DATE_GAP_DAYS = 1

COMPOUNDS = ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET")
RAIN_COMPOUNDS = {"INTERMEDIATE", "WET"}

# Race-control messages that end an open neutralisation, per type.
END_MARKERS = {
    "SC": ("SAFETY CAR IN THIS LAP", "ROLLING START"),
    "VSC": ("VIRTUAL SAFETY CAR ENDING",),
    "RED": ("RESUME",),
}


def fetch(endpoint: str, session_key: int) -> list[dict]:
    return fetch_json(
        f"{BASE_URL}/{endpoint}",
        {"session_key": session_key},
        f"openf1/{session_key}_{endpoint}",
        delay_s=REQUEST_DELAY_S,
        not_found_value=[],  # no rows for this session
    )


def fetch_race_sessions(year: int) -> list[dict]:
    """Grand Prix sessions only: no sprints, nothing cancelled."""
    sessions = fetch_json(
        f"{BASE_URL}/sessions",
        {"year": year, "session_name": "Race"},
        f"openf1/{year}_sessions",
        delay_s=REQUEST_DELAY_S,
    )
    return [s for s in sessions if not s.get("is_cancelled")]


def match_session(race: dict, sessions: list[dict]) -> dict | None:
    """Find the OpenF1 session held on (or within a day of) the race date."""
    race_day = date.fromisoformat(race["date"])

    def gap(session: dict) -> int:
        return abs((date.fromisoformat(session["date_start"][:10]) - race_day).days)

    best = min(sessions, key=gap, default=None)
    return best if best is not None and gap(best) <= MAX_DATE_GAP_DAYS else None


def parse_neutralisations(messages: list[dict], last_lap: int) -> list[dict]:
    """Turn race-control messages into (type, lap_start, lap_end) periods.

    SC  : "SAFETY CAR DEPLOYED"          -> "SAFETY CAR IN THIS LAP"
    SC  : "START BEHIND THE SAFETY CAR"  -> "ROLLING START"   (e.g. wet starts)
    VSC : "VIRTUAL SAFETY CAR DEPLOYED"  -> "VIRTUAL SAFETY CAR ENDING"
    RED : flag RED                        -> first "RESUME" message
    Starting a new period closes any open one (e.g. a VSC that turns into a red flag).
    """
    periods = []
    open_period = None

    def close(lap: int) -> None:
        nonlocal open_period
        if open_period:
            open_period["lap_end"] = max(lap, open_period["lap_start"])
            periods.append(open_period)
            open_period = None

    def start(kind: str, lap: int) -> None:
        nonlocal open_period
        close(lap)
        open_period = {"type": kind, "lap_start": lap}

    for msg in messages:
        lap = msg.get("lap_number") or 1
        text = (msg.get("message") or "").upper()

        if msg.get("flag") == "RED":
            start("RED", lap)
        elif "VIRTUAL SAFETY CAR DEPLOYED" in text:
            start("VSC", lap)
        elif "SAFETY CAR DEPLOYED" in text or "START BEHIND THE SAFETY CAR" in text:
            start("SC", lap)
        elif open_period and any(marker in text for marker in END_MARKERS[open_period["type"]]):
            close(lap)

    close(last_lap)  # still open when the race ended
    return periods


def entry_ids_by_number(session_key: int, entries: list[dict], codes: dict[str, str]) -> dict[int, str]:
    """Map OpenF1 car numbers to our entry_ids for one race, via driver codes."""
    entry_by_code = {codes[e["driver_id"]]: e["entry_id"] for e in entries if codes.get(e["driver_id"])}
    mapping = {}
    for driver in fetch("drivers", session_key):
        entry_id = entry_by_code.get(driver.get("name_acronym"))
        if entry_id:
            mapping[driver["driver_number"]] = entry_id
    return mapping


def parse_stints(raw_stints: list[dict], entry_of: dict[int, str]) -> list[dict]:
    stints = []
    for s in raw_stints:
        entry_id = entry_of.get(s["driver_number"])
        if not entry_id or s.get("lap_start") is None:
            continue
        lap_end = s.get("lap_end") or s["lap_start"]
        compound = (s.get("compound") or "").upper()
        stints.append({
            "stint_id": f"{entry_id}_s{s['stint_number']}",
            "entry_id": entry_id,
            "stint_no": s["stint_number"],
            "lap_start": s["lap_start"],
            "lap_end": lap_end,
            "laps": lap_end - s["lap_start"] + 1,
            "tyre_age_start": s.get("tyre_age_at_start"),
            # Unknown compounds are kept as stints but get no ON_COMPOUND edge.
            "compound": compound if compound in COMPOUNDS else None,
        })
    return stints


def parse_pit_stops(raw_pits: list[dict], entry_of: dict[int, str]) -> list[dict]:
    stops = []
    for p in raw_pits:
        entry_id = entry_of.get(p["driver_number"])
        if not entry_id or p.get("lap_number") is None:
            continue
        stops.append({
            "pit_id": f"{entry_id}_p{p['lap_number']}",
            "entry_id": entry_id,
            "lap": p["lap_number"],
            # Time from pit entry to pit exit, including the stationary stop.
            "duration_s": p.get("lane_duration") or p.get("pit_duration"),
        })
    return stops


def race_conditions(race_id: str, weather: list[dict], stints: list[dict]) -> dict:
    """Average temperatures, and wet = someone ran rain tyres."""
    air = [w["air_temperature"] for w in weather if w.get("air_temperature") is not None]
    track = [w["track_temperature"] for w in weather if w.get("track_temperature") is not None]
    return {
        "race_id": race_id,
        "air_temp_avg": round(mean(air), 1) if air else None,
        "track_temp_avg": round(mean(track), 1) if track else None,
        "wet": any(s["compound"] in RAIN_COMPOUNDS for s in stints),
    }


def load_season(year: int, races: list[dict], entries: list[dict], drivers: list[dict]) -> dict[str, list[dict]]:
    """OpenF1 rows for one season, keyed to the Jolpica races/entries given."""
    sessions = fetch_race_sessions(year)
    codes = {d["driver_id"]: d["code"] for d in drivers}
    rows = {"stints": [], "pit_stops": [], "neutralisations": [], "conditions": []}

    for race in races:
        session = match_session(race, sessions)
        if session is None:
            print(f"  no OpenF1 session for {race['race_id']} ({race['name']})")
            continue
        key = session["session_key"]
        race_entries = [e for e in entries if e["race_id"] == race["race_id"]]
        entry_of = entry_ids_by_number(key, race_entries, codes)

        stints = parse_stints(fetch("stints", key), entry_of)
        rows["stints"].extend(stints)
        rows["pit_stops"].extend(parse_pit_stops(fetch("pit", key), entry_of))
        rows["conditions"].append(race_conditions(race["race_id"], fetch("weather", key), stints))
        for i, period in enumerate(parse_neutralisations(fetch("race_control", key), race["total_laps"]), start=1):
            rows["neutralisations"].append({"n_id": f"{race['race_id']}_n{i}", "race_id": race["race_id"], **period})

        unmatched = len(race_entries) - len(entry_of)
        if unmatched:
            print(f"  {race['race_id']}: {unmatched} entries had no OpenF1 driver match")
    return rows
