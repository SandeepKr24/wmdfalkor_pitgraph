"""Fetch race results from Jolpica-F1 (the Ergast successor) and turn them into rows.

Produces plain dicts for: Season, Circuit, Race, Driver, Team, Entry.
Stints, pit stops, neutralisations and weather come from OpenF1 (openf1.py).
"""

from pitgraph.ingest.http import fetch_json

SEASONS = (2023, 2024, 2025)
BASE_URL = "https://api.jolpi.ca/ergast/f1"
PAGE_SIZE = 100  # Jolpica's maximum

# Jolpica has no circuit type or length, so we add them by hand.
# "street" = public roads or a temporary layout; "permanent" = purpose-built track.
CIRCUIT_INFO = {
    "albert_park": ("street", 5.278),
    "americas": ("permanent", 5.513),
    "bahrain": ("permanent", 5.412),
    "baku": ("street", 6.003),
    "catalunya": ("permanent", 4.657),
    "hungaroring": ("permanent", 4.381),
    "imola": ("permanent", 4.909),
    "interlagos": ("permanent", 4.309),
    "jeddah": ("street", 6.174),
    "losail": ("permanent", 5.419),
    "marina_bay": ("street", 4.940),
    "miami": ("street", 5.412),
    "monaco": ("street", 3.337),
    "monza": ("permanent", 5.793),
    "red_bull_ring": ("permanent", 4.318),
    "rodriguez": ("permanent", 4.304),
    "shanghai": ("permanent", 5.451),
    "silverstone": ("permanent", 5.891),
    "spa": ("permanent", 7.004),
    "suzuka": ("permanent", 5.807),
    "vegas": ("street", 6.201),
    "villeneuve": ("street", 4.361),
    "yas_marina": ("permanent", 5.281),
    "zandvoort": ("permanent", 4.259),
}


def fetch_races(year: int) -> list[dict]:
    """The season calendar: one entry per race, with its circuit."""
    data = fetch_json(f"{BASE_URL}/{year}/races.json", {"limit": PAGE_SIZE}, f"jolpica/{year}_races")
    return data["MRData"]["RaceTable"]["Races"]


def fetch_results(year: int) -> dict[int, list[dict]]:
    """All race results for a season, grouped by round.

    Results are paginated by result row, so one race can be split across
    two pages. Grouping by round stitches them back together.
    """
    by_round: dict[int, list[dict]] = {}
    offset = 0
    while True:
        data = fetch_json(
            f"{BASE_URL}/{year}/results.json",
            {"limit": PAGE_SIZE, "offset": offset},
            f"jolpica/{year}_results_{offset}",
        )
        mr = data["MRData"]
        for race in mr["RaceTable"]["Races"]:
            by_round.setdefault(int(race["round"]), []).extend(race["Results"])
        offset += PAGE_SIZE
        if offset >= int(mr["total"]):
            return by_round


def parse_circuit(circuit: dict) -> dict:
    circuit_id = circuit["circuitId"]
    if circuit_id not in CIRCUIT_INFO:
        raise ValueError(f"Add {circuit_id!r} to CIRCUIT_INFO")
    circuit_type, length_km = CIRCUIT_INFO[circuit_id]
    return {
        "circuit_id": circuit_id,
        "name": circuit["circuitName"],
        "country": circuit["Location"]["country"],
        "type": circuit_type,
        "length_km": length_km,
    }


def parse_driver(result: dict) -> dict:
    driver = result["Driver"]
    return {
        "driver_id": driver["driverId"],
        "code": driver.get("code"),
        "name": f"{driver['givenName']} {driver['familyName']}",
        "number": int(driver.get("permanentNumber") or result["number"]),
    }


def parse_entry(rid: str, result: dict) -> dict:
    """One driver at one race."""
    driver_id = result["Driver"]["driverId"]
    return {
        "entry_id": f"{rid}_{driver_id}",
        "race_id": rid,
        "driver_id": driver_id,
        "team_id": result["Constructor"]["constructorId"],
        "grid": int(result["grid"]),  # 0 = started from the pit lane
        "finish_pos": int(result["position"]),
        "points": float(result["points"]),
        "status": result["status"],
    }


def load_season(year: int) -> dict[str, list[dict]]:
    """Fetch one season and return rows keyed by node label."""
    results_by_round = fetch_results(year)
    rows = {"circuits": {}, "races": [], "drivers": {}, "teams": {}, "entries": []}

    for race in fetch_races(year):
        round_no = int(race["round"])
        results = results_by_round.get(round_no, [])
        if not results:
            continue  # race not run (or cancelled)

        rid = f"{year}_{round_no}"
        circuit = parse_circuit(race["Circuit"])
        rows["circuits"][circuit["circuit_id"]] = circuit
        rows["races"].append({
            "race_id": rid,
            "year": year,
            "round": round_no,
            "name": race["raceName"],
            "date": race["date"],
            "circuit_id": circuit["circuit_id"],
            # The winner's lap count is the race distance actually run.
            "total_laps": max(int(r["laps"]) for r in results),
        })

        for result in results:
            driver = parse_driver(result)
            rows["drivers"][driver["driver_id"]] = driver
            team = result["Constructor"]
            rows["teams"][team["constructorId"]] = {"team_id": team["constructorId"], "name": team["name"]}
            rows["entries"].append(parse_entry(rid, result))

    # De-duplicated dicts back to lists.
    for key in ("circuits", "drivers", "teams"):
        rows[key] = list(rows[key].values())
    return rows
