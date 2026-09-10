"""Geographic region taxonomy + address→region resolution (platform placement).

A global, best-practice set of regions (mirroring how the major clouds carve the
world) so the platform is ready for worldwide distribution. Signups are routed to
a region by the country + subdivision the customer provides; each region is served
by a Cluster. Only US + Canada accept signups today (``ACCEPTED_COUNTRIES``); the
rest of the taxonomy exists but its regions ship with ``signup_enabled=False`` so
they can be turned on once a cluster serves them.
"""

from __future__ import annotations

# The global region taxonomy. `signup_default` = whether this region accepts
# signups when first seeded (only the two North-American regions, initially).
REGION_TAXONOMY: list[dict] = [
    {"code": "nam-east", "name": "North America East", "geo_zone": "north-america",
     "sort_order": 10, "signup_default": True},
    {"code": "nam-west", "name": "North America West", "geo_zone": "north-america",
     "sort_order": 20, "signup_default": True},
    {"code": "sa-east", "name": "South America East", "geo_zone": "south-america",
     "sort_order": 30, "signup_default": False},
    {"code": "eu-west", "name": "Europe West", "geo_zone": "europe",
     "sort_order": 40, "signup_default": False},
    {"code": "eu-central", "name": "Europe Central", "geo_zone": "europe",
     "sort_order": 50, "signup_default": False},
    {"code": "uk", "name": "United Kingdom & Ireland", "geo_zone": "europe",
     "sort_order": 60, "signup_default": False},
    {"code": "me", "name": "Middle East", "geo_zone": "middle-east",
     "sort_order": 70, "signup_default": False},
    {"code": "africa", "name": "Africa", "geo_zone": "africa",
     "sort_order": 80, "signup_default": False},
    {"code": "apac-south", "name": "Asia Pacific South (India)", "geo_zone": "asia-pacific",
     "sort_order": 90, "signup_default": False},
    {"code": "apac-southeast", "name": "Asia Pacific Southeast", "geo_zone": "asia-pacific",
     "sort_order": 100, "signup_default": False},
    {"code": "apac-northeast", "name": "Asia Pacific Northeast", "geo_zone": "asia-pacific",
     "sort_order": 110, "signup_default": False},
    {"code": "oceania", "name": "Australia & New Zealand", "geo_zone": "oceania",
     "sort_order": 120, "signup_default": False},
]

REGION_CODES = {r["code"] for r in REGION_TAXONOMY}

# Countries whose signups we accept right now (ISO-3166 alpha-2, upper).
ACCEPTED_COUNTRIES = {"US", "CA"}

# Default region for each continental zone (fallback when a finer split isn't
# available for that country).
_ZONE_DEFAULT_REGION = {
    "north-america": "nam-east",
    "south-america": "sa-east",
    "europe": "eu-west",
    "middle-east": "me",
    "africa": "africa",
    "asia-pacific": "apac-southeast",
    "oceania": "oceania",
}

# US states / territories routed to the WEST region; everything else → East.
_US_WEST = {"WA", "OR", "CA", "NV", "ID", "UT", "AZ", "MT", "WY", "CO", "NM",
            "AK", "HI"}
# Canadian provinces/territories routed WEST; everything else → East.
_CA_WEST = {"BC", "AB", "SK", "YT", "NT"}

# A pragmatic country → continental-zone map (majors; extend as needed). Unknown
# countries fall back to Europe West as a neutral default.
_COUNTRY_ZONE = {
    # North America
    "US": "north-america", "CA": "north-america", "MX": "north-america",
    "GT": "north-america", "CR": "north-america", "PA": "north-america",
    # South America
    "BR": "south-america", "AR": "south-america", "CL": "south-america",
    "CO": "south-america", "PE": "south-america", "UY": "south-america",
    # United Kingdom & Ireland
    "GB": "europe", "IE": "europe",
    # Europe (continental)
    "DE": "europe", "FR": "europe", "ES": "europe", "IT": "europe", "NL": "europe",
    "SE": "europe", "NO": "europe", "FI": "europe", "DK": "europe", "PL": "europe",
    "PT": "europe", "BE": "europe", "AT": "europe", "CH": "europe", "CZ": "europe",
    # Middle East
    "AE": "middle-east", "SA": "middle-east", "IL": "middle-east", "TR": "middle-east",
    "QA": "middle-east", "KW": "middle-east", "BH": "middle-east",
    # Africa
    "ZA": "africa", "NG": "africa", "KE": "africa", "EG": "africa", "MA": "africa",
    # Asia Pacific
    "IN": "asia-pacific", "SG": "asia-pacific", "MY": "asia-pacific", "ID": "asia-pacific",
    "TH": "asia-pacific", "PH": "asia-pacific", "VN": "asia-pacific",
    "JP": "asia-pacific", "KR": "asia-pacific", "HK": "asia-pacific", "TW": "asia-pacific",
    "CN": "asia-pacific",
    # Oceania
    "AU": "oceania", "NZ": "oceania",
}

# Finer zone splits for countries that map to more than one region.
_APAC_NORTHEAST = {"JP", "KR", "HK", "TW", "CN"}
_APAC_SOUTH = {"IN"}
_UK = {"GB", "IE"}


def normalize_country(country: str) -> str:
    return (country or "").strip().upper()[:2]


def normalize_subdivision(sub: str) -> str:
    """State/province code, upper, without a country prefix (e.g. 'US-CA' → 'CA')."""
    s = (sub or "").strip().upper()
    if "-" in s:
        s = s.rsplit("-", 1)[-1]
    return s


def is_accepted(country: str) -> bool:
    return normalize_country(country) in ACCEPTED_COUNTRIES


def resolve_region(country: str, subdivision: str = "") -> str:
    """Map an address (country + optional state/province) to a region code."""
    c = normalize_country(country)
    sub = normalize_subdivision(subdivision)
    if c == "US":
        return "nam-west" if sub in _US_WEST else "nam-east"
    if c == "CA":
        return "nam-west" if sub in _CA_WEST else "nam-east"
    zone = _COUNTRY_ZONE.get(c, "europe")
    if zone == "asia-pacific":
        if c in _APAC_SOUTH:
            return "apac-south"
        if c in _APAC_NORTHEAST:
            return "apac-northeast"
        return "apac-southeast"
    if zone == "europe" and c in _UK:
        return "uk"
    return _ZONE_DEFAULT_REGION.get(zone, "eu-west")
