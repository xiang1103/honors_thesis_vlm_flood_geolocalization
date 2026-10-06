"""Flood relevance and New York signals for video records, source-agnostic.

Two things are computed from a video's TEXT (title, tags, description) and,
for New York, its channel and coordinates. Neither drops anything: every video
is written with its labels, and a reader picks the cut. Design and the
reasoning behind each rule: scraping/video_scraping/design.md.

  score_flood_text(...)  signal 1 -- `flood_text_score` 0.0-1.0 and the
                         matched terms (`flood_text_hits`) so it can be audited.
                         Says the video is ABOUT a flood, not that flooding is
                         visible; that is the VLM's job on the thumbnails.
  label_new_york(...)    `in_ny`, `in_nyc`, `ny_basis`, `ny_places`.
                         `None` means unknown -- the common case -- and is not
                         the same as False.
  match_flood_event(...) signal 2 -- the known NY flood an upload follows.

The vocabulary extends verification/verify_text.py (STRONG / WEAK / METAPHOR)
so news and video are scored on one set of flood terms.
"""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "verification"))
from verify_text import METAPHOR, STRONG, WEAK  # noqa: E402


# --------------------------------------------------------------------------
# Signal 1: flood relevance from text
# --------------------------------------------------------------------------

#: Video-specific strong phrasing, on top of verify_text.STRONG.
VIDEO_STRONG = re.compile(
    r"(\bflooded\s+(street|road|roads|streets|highway|parkway|expressway|subway|basement|tunnel)s?\b|"
    r"\bdriving\s+(through|thru|in)\s+(the\s+)?(flood|deep\s+water|high\s+water)|"
    r"\bwater\s+(rising|is\s+rising|up\s+to)\b|\bcars?\s+(stuck|stranded|floating)\b|"
    r"\bstreet\s+turned\s+(into\s+)?(a\s+)?river\b|#\s?(flood|flooding|flashflood|nycflood)\w*)",
    re.I)

#: Ground-level / street context. Not flood evidence by itself, so it counts
#: like WEAK: support for a strong hit, never a substitute for one.
VIDEO_WEAK = re.compile(
    r"\b(dash\s?cam|dashcam|storm\s+drain|sewer|manhole|basement|underpass|"
    r"hydroplan\w*|nor'?easter|remnants\s+of|tropical\s+storm|heavy\s+rain|downpour)\b",
    re.I)

#: Any hit forces the score to 0. These are where "flood" in a YouTube title
#: most often does not mean water in a street: games and their flood
#: mechanics, CG simulations, toy dam-breaks, film/music, insurance and
#: real-estate explainers, and "flood light(s)".
HARD_NEGATIVE = re.compile(
    r"\b(minecraft|roblox|fortnite|cities:?\s+skylines|flood\s+escape|"
    r"natural\s+disaster\s+survival|gta\s?(v|5|iv|6)?|gameplay|let'?s\s+play|"
    r"flood\s?fill|flood\s?lights?|noah'?s\s+ark|"
    r"official\s+(trailer|music\s+video|video|audio)|movie\s+trailer|full\s+movie|lyrics?\b|"
    r"water\s+simulation|simulator|blender|houdini|lego|"
    r"flood\s+insurance|house\s+tour|home\s+tour|for\s+sale|real\s+estate|realtor)",
    re.I)

#: Penalised, not zeroed. Compilations are usually real floods but stitched
#: from other people's clips with no reliable place or date.
SOFT_NEGATIVE = re.compile(
    r"\b(compilation|top\s+\d+|most\s+(shocking|insane|terrifying|dangerous)|"
    r"caught\s+on\s+camera\s+compilation|you\s+won'?t\s+believe)\b",
    re.I)

#: `flood_text_relevant` = score >= this. Owner's choice, 2026-10-06 (was an
#: unstored 0.4 in reports). Changing it needs no API search: re-run
#: `youtube_scrape.py --no-search` to relabel every video (~1 unit / 50 videos).
FLOOD_TEXT_THRESHOLD = 0.3

#: YouTube category ids that rarely hold real flood footage.
PENALISED_CATEGORIES = {
    "1": "Film & Animation",
    "10": "Music",
    "20": "Gaming",
    "24": "Entertainment",
}

#: Description lines that are pure boilerplate: links, social handles,
#: subscribe/merch/sponsor copy. Stripped before scoring.
BOILERPLATE_LINE = re.compile(
    r"^\s*(https?://\S+|www\.\S+|@\w+|#\w+(\s+#\w+)*|"
    r".*\b(subscribe|follow\s+(us|me)|instagram|tiktok|twitter|facebook|patreon|"
    r"merch|sponsor|affiliate|business\s+inquir\w*|licensing|copyright)\b.*)\s*$",
    re.I)


def _hits(pattern: re.Pattern, text: str) -> list[str]:
    return [m.group(0).strip() for m in pattern.finditer(text)]


def clean_description(description: str | None) -> str:
    """The description without boilerplate lines."""
    lines = (description or "").splitlines()
    return "\n".join(l for l in lines if not BOILERPLATE_LINE.match(l))


def score_flood_text(title: str | None, tags: Iterable[str] | None,
                     description: str | None, category_id: str | None) -> tuple[float, dict[str, list[str]]]:
    """Signal 1. Returns (score 0.0-1.0, {strong, weak, negative, soft_negative}).

    Weights reflect where uploaders put the event: the title first, then tags,
    then a (boilerplate-stripped) description. A strong hit in the title is
    worth more than any amount of description density -- descriptions are long
    and often copied between a channel's videos.
    """
    title = title or ""
    tag_text = " ; ".join(tags or [])
    desc = clean_description(description)
    everything = f"{title}\n{tag_text}\n{desc}"

    def strong(text: str) -> list[str]:
        return _hits(STRONG, text) + _hits(VIDEO_STRONG, text)

    strong_title, strong_tags, strong_desc = strong(title), strong(tag_text), strong(desc)
    weak = _hits(WEAK, everything) + _hits(VIDEO_WEAK, everything)
    # Hard negatives count only where the uploader says what the video IS:
    # title and tags. In a description they are usually context -- news
    # descriptions mention flood insurance, broker channels carry "real
    # estate" boilerplate -- and zeroed 32 genuine flood videos in the first
    # run (2026-10-06), so there they are a soft negative instead.
    hard = _hits(HARD_NEGATIVE, f"{title}\n{tag_text}")
    soft = _hits(SOFT_NEGATIVE, everything) + _hits(HARD_NEGATIVE, desc)
    metaphor = _hits(METAPHOR, everything)
    if category_id in PENALISED_CATEGORIES:
        soft.append(f"category:{PENALISED_CATEGORIES[category_id]}")

    hits = {
        "strong": strong_title + strong_tags + strong_desc,
        "weak": weak,
        "negative": hard,
        "soft_negative": soft + metaphor,
    }
    if hard:
        return 0.0, hits

    s = 0.0
    if strong_title:
        s += 0.45
    if strong_tags:
        s += 0.15
    s += min(len(strong_desc), 5) / 5 * 0.20
    s += min(len(weak), 5) / 5 * 0.10
    if strong_title and (strong_tags or strong_desc):
        s += 0.10                              # the event is named in more than one place
    s -= min(0.15 * len(soft), 0.45)
    s -= min(0.30 * len(metaphor), 0.60)       # "a flood of memories" cancels a title hit
    return round(max(0.0, min(1.0, s)), 3), hits


# --------------------------------------------------------------------------
# New York
# --------------------------------------------------------------------------

def _alternation(names: Iterable[str], upper: bool = False) -> str:
    """Longest first, so "Far Rockaway" wins over "Rockaway". `upper` adds
    each name in capitals too, for the case-sensitive marker-only pattern
    ("QUEENS FLOODING" titles)."""
    names = set(names)
    if upper:
        names |= {n.upper() for n in names}
    return "|".join(re.escape(n).replace(r"\ ", r"\s+")
                    for n in sorted(names, key=len, reverse=True))


#: Not the NYC place: Manhattan Beach (CA), Manhattan (KS), Brooklyn Park (MN).
NOT_THIS_PLACE = r"(?!\s+(Beach|Kansas|KS|Park,?\s+MN)\b)"


#: Things that say "New York" by themselves. "NY" is matched separately and
#: case-sensitively: lowercase "ny" is too common in other languages and handles.
NY_MARKER = re.compile(r"\bN\.Y\.|\bnyc\b|\bnew\s+york(ers?)?\b", re.I)
NY_MARKER_STRICT_NY = re.compile(r"\bNY\b")          # applied without re.I
NYC_MARKER = re.compile(r"\bNYC\b|\bnew\s+york\s+city\b|\bfive\s+boroughs\b", re.I)

#: NYC names unambiguous enough to count WITHOUT a NY marker.
NYC_STANDALONE = [
    "Brooklyn", "Bronx", "Staten Island", "Manhattan", "Harlem", "East Harlem",
    "Far Rockaway", "Rockaway Beach", "Rockaway Park", "Rockaways", "Howard Beach",
    "Broad Channel", "Canarsie", "Coney Island", "Sheepshead Bay", "Gowanus",
    "Bushwick", "Greenpoint", "Williamsburg Brooklyn", "Bed-Stuy", "Bedford-Stuyvesant",
    "Crown Heights", "Park Slope", "Sunset Park", "Bay Ridge", "Red Hook Brooklyn",
    "Astoria Queens", "Long Island City", "Jackson Heights", "East Elmhurst",
    "Corona Queens", "Flushing Queens", "Ozone Park", "Jamaica Queens",
    "St. Albans Queens", "Rosedale Queens",
    "Springfield Gardens", "Arverne", "Edgemere", "Hunts Point", "Mott Haven",
    "City Island", "Pelham Bay", "Co-op City", "Riverdale Bronx", "Washington Heights",
    "Inwood Manhattan", "Lower East Side", "Upper West Side", "Upper East Side",
    "Midtown Manhattan", "Tribeca", "Battery Park", "Chinatown NYC",
    # roads and transit that exist only in NYC
    "BQE", "Brooklyn-Queens Expressway", "FDR Drive", "Major Deegan", "Cross Bronx",
    "Van Wyck", "Grand Central Parkway", "Belt Parkway", "Bronx River Parkway",
    "Hutchinson River Parkway", "Henry Hudson Parkway", "Harlem River Drive",
    "Jackie Robinson Parkway", "Whitestone Expressway", "Clearview Expressway",
    "Staten Island Expressway", "NYC subway", "MTA subway",
]

#: Rest of New York State, unambiguous enough to count without a marker.
NY_STANDALONE = [
    "Long Island", "Nassau County", "Suffolk County", "Westchester", "Hudson Valley",
    "Catskills", "Adirondacks", "Finger Lakes", "Upstate New York", "Hamptons",
    "Montauk", "Southampton NY", "Patchogue", "Freeport Long Island", "Massapequa",
    "Lindenhurst", "Babylon Long Island", "Mastic Beach", "Fire Island", "Poughkeepsie",
    "Saugerties", "Phoenicia", "Margaretville", "Ithaca", "Watkins Glen", "Lake Placid",
    "Long Island Expressway", "Southern State Parkway", "Northern State Parkway",
    "Sunrise Highway", "Taconic Parkway", "Saw Mill Parkway", "Sprain Brook Parkway",
    "NYS Thruway", "New York State Thruway", "LIRR", "Metro-North",
]

#: Counted ONLY alongside a NY marker (or a standalone hit): each is also a
#: common word, another state's town, or another country. Extended at run
#: time with every `place` in data/mycoast.json -- 232 NY localities, many of
#: them (Mexico, Jordan, Memphis, Geneva, Cairo, Kent) far more famous elsewhere.
NYC_NEEDS_MARKER = [
    "Queens", "Astoria", "Flushing", "Jamaica", "Corona", "Woodside", "Sunnyside",
    "Williamsburg", "Red Hook", "Chelsea", "SoHo", "Tribeca", "Rockaway", "Inwood",
    "Riverdale", "Gravesend", "Flatbush", "Kensington", "Bensonhurst",
    "LIC", "Seagate", "Oakwood", "Richmond", "Elmhurst", "Forest Hills", "Kew Gardens",
    "Richmond Hill", "Hollis",
]
NY_NEEDS_MARKER = [
    "Rochester", "Buffalo", "Albany", "Syracuse", "Utica", "Binghamton", "Kingston",
    "Newburgh", "Yonkers", "White Plains", "New Rochelle", "Mamaroneck", "Ossining",
    "Beacon", "Hyde Park", "Rhinebeck", "Oswego", "Watertown", "Elmira", "Corning",
    "Geneva", "Schenectady", "Troy", "Saratoga", "Lake George", "Niagara Falls",
    "Freeport", "Long Beach", "Babylon", "Oyster Bay", "Glen Cove", "Port Washington",
]

#: NYC counties as MyCoast writes them -- which MyCoast places are in the city.
NYC_COUNTIES = {"Bronx County, NY", "Kings County, NY", "New York County, NY",
                "Queens County, NY", "Richmond County, NY"}

#: Evidence of ELSEWHERE. Only used to set in_ny=False when there is no NY
#: hit at all. Not "Washington" (Washington Heights) or "Jersey" alone.
OTHER_PLACES = re.compile(
    r"\b(new\s+jersey|connecticut|pennsylvania|massachusetts|vermont|maryland|virginia|"
    r"west\s+virginia|north\s+carolina|south\s+carolina|georgia|florida|alabama|mississippi|"
    r"louisiana|texas|oklahoma|kansas|missouri|arkansas|tennessee|kentucky|ohio|michigan|"
    r"indiana|illinois|wisconsin|minnesota|iowa|nebraska|dakota|montana|wyoming|colorado|"
    r"utah|arizona|new\s+mexico|nevada|california|oregon|idaho|alaska|hawaii|"
    r"houston|miami|new\s+orleans|los\s+angeles|chicago|philadelphia|boston|hoboken|"
    r"jersey\s+city|newark|"
    r"canada|uk|england|london|scotland|wales|ireland|india|pakistan|bangladesh|china|japan|"
    r"philippines|indonesia|vietnam|thailand|malaysia|nigeria|brazil|mexico\s+city|"
    r"australia|new\s+zealand|germany|italy|spain|france|dubai|saudi|libya|"
    r"south\s+africa|kenya|nepal|sri\s+lanka)\b",
    re.I)
OTHER_STATE_ABBREV = re.compile(
    r",\s*(NJ|CT|PA|MA|VT|MD|VA|NC|SC|GA|FL|AL|MS|LA|TX|OK|KS|MO|AR|TN|KY|OH|MI|IN|IL|"
    r"WI|MN|IA|NE|ND|SD|MT|WY|CO|UT|AZ|NM|NV|CA|OR|WA|ID|AK|HI|DE|RI|NH|ME)\b")

#: Channels whose coverage area is New York State alone (or a part of it):
#: their upload is NY unless coordinates say otherwise.
NY_ONLY_CHANNELS = re.compile(
    r"\b(NY1|Spectrum\s+News\s+NY1|News\s?12\s+(Long\s+Island|Brooklyn|Bronx|Westchester|"
    r"Hudson\s+Valley)|WGRZ|WKBW|7\s+News\s+WKBW|News\s?4\s+Buffalo|WIVB|WHAM|News10NBC|"
    r"WROC|WSYR|CNYCentral|WNYT|NEWS10\s+ABC|WRGB|CBS6\s+Albany|Spectrum\s+News\s+1?\s*"
    r"(Albany|Buffalo|Rochester|Syracuse|Central\s+NY|Capital\s+Region|Hudson\s+Valley))\b",
    re.I)
#: New York City stations that also cover New Jersey and Connecticut: NY unless
#: the text names somewhere else.
TRISTATE_CHANNELS = re.compile(
    r"\b(PIX11|PIX\s?11|ABC7\s?NY|Eyewitness\s+News\s+ABC7NY|NBC\s+New\s+York|CBS\s+New\s+York|"
    r"FOX\s?5\s+New\s+York|News\s?12(\s+(New\s+Jersey|Connecticut))?|SNY)\b",
    re.I)


class Gazetteer:
    """Place-name matcher for New York. Build once, apply per video."""

    def __init__(self, mycoast_places: Iterable[tuple[str, str]] = ()):
        """`mycoast_places`: (place, county) pairs from data/mycoast.json."""
        nyc_marker_only = set(NYC_NEEDS_MARKER)
        ny_marker_only = set(NY_NEEDS_MARKER)
        standalone = set(NYC_STANDALONE) | set(NY_STANDALONE)
        for place, county in mycoast_places:
            place = (place or "").strip()
            # Skip street fragments ("E 16th Rd") and bare state labels.
            if len(place) < 4 or re.search(r"\d|\b(Rd|Ave|Dr|Way|St)\b|^(NY|New York)$", place):
                continue
            if place in standalone:
                continue
            (nyc_marker_only if county in NYC_COUNTIES else ny_marker_only).add(place)
        self._nyc = set(NYC_STANDALONE) | nyc_marker_only
        self.standalone = re.compile(rf"\b({_alternation(standalone)})\b{NOT_THIS_PLACE}", re.I)
        self.marker_only = re.compile(
            rf"\b({_alternation(nyc_marker_only | ny_marker_only, upper=True)})\b{NOT_THIS_PLACE}")
        self._nyc_lower = {n.lower() for n in self._nyc}

    def is_nyc(self, name: str) -> bool:
        return re.sub(r"\s+", " ", name).lower() in self._nyc_lower

    def match(self, text: str, ny_context: bool = False) -> tuple[list[str], bool]:
        """(NY place names found, whether a NY marker was present).

        Marker-only names are matched CASE-SENSITIVELY ("Queens", not "queens";
        "Jamaica" the neighbourhood is capitalised either way, which is why it
        also needs the marker) and only count when a marker, a standalone
        name, or outside `ny_context` (a NY-only channel) is present.
        """
        marker = bool(NY_MARKER_STRICT_NY.search(text) or NY_MARKER.search(text))
        found = [m.group(0) for m in self.standalone.finditer(text)]
        if marker or found or ny_context:
            found += [m.group(0) for m in self.marker_only.finditer(text)]
        seen: dict[str, str] = {}
        for name in found:
            seen.setdefault(re.sub(r"\s+", " ", name).lower(), re.sub(r"\s+", " ", name))
        return list(seen.values()), marker


def label_new_york(text: str, channel_title: str | None, gazetteer: Gazetteer,
                   coord_in_ny: bool | None, coord_in_nyc: bool | None,
                   event_date: str | None) -> dict[str, Any]:
    """`in_ny`, `in_nyc`, `ny_basis`, `ny_places` for one video.

    Precedence, strongest first:
      coordinates  -> decide both, nothing below can overturn them
      place names / NY marker / NY-only channel -> in_ny True
      tri-state NYC channel -> in_ny True unless the text names elsewhere
      text names elsewhere and nothing NY -> in_ny False
      otherwise None
    `coord_in_ny` is None when the video has no coordinates. The event date is
    recorded in `ny_basis` as corroboration and never decides anything alone.
    """
    basis: list[str] = []
    channel = channel_title or ""
    ny_only_channel = bool(NY_ONLY_CHANNELS.search(channel))
    tristate_channel = bool(TRISTATE_CHANNELS.search(channel))
    places, marker = gazetteer.match(text, ny_context=ny_only_channel)
    nyc_text = bool(NYC_MARKER.search(text)) or any(gazetteer.is_nyc(p) for p in places)
    elsewhere = bool(OTHER_PLACES.search(text) or OTHER_STATE_ABBREV.search(text))

    if coord_in_ny is not None:
        basis.append("coordinates")
    if places or marker:
        basis.append("gazetteer")
    if ny_only_channel or tristate_channel:
        basis.append("channel")
    if event_date:
        basis.append("event_date")

    if coord_in_ny is not None:
        in_ny: bool | None = coord_in_ny
        in_nyc: bool | None = bool(coord_in_nyc)
    else:
        if places or marker or ny_only_channel:
            in_ny = True
        elif tristate_channel and not elsewhere:
            in_ny = True
        elif elsewhere:
            in_ny = False
        else:
            in_ny = None
        if in_ny is False:
            in_nyc = False
        elif nyc_text:
            in_nyc = True
        elif in_ny and places:           # NY places named, none of them in the city
            in_nyc = False
        else:
            in_nyc = None
    return {"in_ny": in_ny, "in_nyc": in_nyc, "ny_basis": basis, "ny_places": places}


# --------------------------------------------------------------------------
# Signal 2: known flood events
# --------------------------------------------------------------------------

#: Major NY floods, by local date. MyCoast days are added at run time.
NY_FLOOD_EVENTS = {
    "2011-08-28": "Hurricane Irene",
    "2011-09-08": "Tropical Storm Lee",
    "2012-10-29": "Hurricane Sandy",
    "2021-08-22": "Tropical Storm Henri",
    "2021-09-01": "Remnants of Hurricane Ida",
    "2023-07-10": "Hudson Valley flash floods",
    "2023-09-29": "NYC flash flood",
    "2024-08-19": "Long Island flash flood",
}
#: An upload this many days after an event is still "about" it.
EVENT_WINDOW_DAYS = 3
#: A MyCoast day counts as an event with at least this many reports (96 days
#: as of 2026-10-05; at 3 it is 193, mostly ordinary king-tide reporting).
MYCOAST_EVENT_MIN_REPORTS = 5


def flood_event_days(mycoast_rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Local date -> event name: the hand list plus busy MyCoast days."""
    per_day: dict[str, int] = {}
    for row in mycoast_rows:
        day = (row.get("local_time") or "")[:10]
        if day >= "2000":                     # pre-2000 MyCoast dates are errors
            per_day[day] = per_day.get(day, 0) + 1
    events = {d: f"MyCoast: {n} NY reports" for d, n in per_day.items()
              if n >= MYCOAST_EVENT_MIN_REPORTS}
    events.update(NY_FLOOD_EVENTS)
    return events


def match_flood_event(when_utc: str | None, events: dict[str, str]) -> str | None:
    """The latest event day on or up to EVENT_WINDOW_DAYS before `when_utc`.

    Compared on the UTC date: an evening upload in New York is already the
    next day in UTC, which the window absorbs.
    """
    if not when_utc:
        return None
    try:
        day = date.fromisoformat(when_utc[:10])
    except ValueError:
        return None
    for back in range(EVENT_WINDOW_DAYS + 1):
        d = (day - timedelta(days=back)).isoformat()
        if d in events:
            return d
    return None
