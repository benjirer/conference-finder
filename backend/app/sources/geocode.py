"""Lightweight geocoding for venue locations.

Most conferences rotate through a fairly small set of host cities — ~150
cover the bulk of ACM/IEEE/USENIX flagships. A static dict + substring match
is fast, deterministic, free, and works offline. Anything that doesn't match
just stays without coordinates (the world-map UI shows it as a distance-unknown
row at the bottom of the sort order).

Coordinates are city-center, suitable for the "show me closest venues" feature
where 50–500 km accuracy is more than enough.
"""
from __future__ import annotations

from ..db import SessionLocal
from ..models import Conference

# (lowercased substring → (latitude, longitude))
# Order matters: longer/more-specific matches go first.
CITIES: list[tuple[str, tuple[float, float]]] = [
    # Switzerland / Germany / Austria
    ("zurich",        ( 47.3769,   8.5417)),
    ("zürich",   ( 47.3769,   8.5417)),
    ("geneva",        ( 46.2044,   6.1432)),
    ("lausanne",      ( 46.5197,   6.6323)),
    ("basel",         ( 47.5596,   7.5886)),
    ("bern",          ( 46.9480,   7.4474)),
    ("vienna",        ( 48.2082,  16.3738)),
    ("salzburg",      ( 47.8095,  13.0550)),
    ("graz",          ( 47.0707,  15.4395)),
    ("berlin",        ( 52.5200,  13.4050)),
    ("munich",        ( 48.1351,  11.5820)),
    ("münchen",  ( 48.1351,  11.5820)),
    ("hamburg",       ( 53.5511,   9.9937)),
    ("frankfurt",     ( 50.1109,   8.6821)),
    ("stuttgart",     ( 48.7758,   9.1829)),
    ("cologne",       ( 50.9375,   6.9603)),
    ("dresden",       ( 51.0504,  13.7373)),
    ("aachen",        ( 50.7753,   6.0839)),
    ("kaiserslautern",( 49.4401,   7.7491)),
    # France / Benelux / UK / Ireland
    ("paris",         ( 48.8566,   2.3522)),
    ("lyon",          ( 45.7640,   4.8357)),
    ("marseille",     ( 43.2965,   5.3698)),
    ("nice",          ( 43.7102,   7.2620)),
    ("toulouse",      ( 43.6047,   1.4442)),
    ("nantes",        ( 47.2184,  -1.5536)),
    ("amsterdam",     ( 52.3676,   4.9041)),
    ("delft",         ( 52.0116,   4.3571)),
    ("rotterdam",     ( 51.9244,   4.4777)),
    ("eindhoven",     ( 51.4416,   5.4697)),
    ("brussels",      ( 50.8503,   4.3517)),
    ("antwerp",       ( 51.2194,   4.4025)),
    ("luxembourg",    ( 49.6116,   6.1319)),
    ("london",        ( 51.5074,  -0.1278)),
    ("cambridge, uk", ( 52.2053,   0.1218)),
    ("oxford",        ( 51.7520,  -1.2577)),
    ("edinburgh",     ( 55.9533,  -3.1883)),
    ("glasgow",       ( 55.8642,  -4.2518)),
    ("manchester",    ( 53.4808,  -2.2426)),
    ("dublin",        ( 53.3498,  -6.2603)),
    # Iberia / Italy / Greece / Nordic
    ("madrid",        ( 40.4168,  -3.7038)),
    ("barcelona",     ( 41.3851,   2.1734)),
    ("lisbon",        ( 38.7223,  -9.1393)),
    ("porto",         ( 41.1579,  -8.6291)),
    ("rome",          ( 41.9028,  12.4964)),
    ("milan",         ( 45.4642,   9.1900)),
    ("turin",         ( 45.0703,   7.6869)),
    ("florence",      ( 43.7696,  11.2558)),
    ("naples",        ( 40.8518,  14.2681)),
    ("pisa",          ( 43.7228,  10.4017)),
    ("bologna",       ( 44.4949,  11.3426)),
    ("athens",        ( 37.9838,  23.7275)),
    ("thessaloniki",  ( 40.6401,  22.9444)),
    ("copenhagen",    ( 55.6761,  12.5683)),
    ("stockholm",     ( 59.3293,  18.0686)),
    ("gothenburg",    ( 57.7089,  11.9746)),
    ("oslo",          ( 59.9139,  10.7522)),
    ("helsinki",      ( 60.1699,  24.9384)),
    ("reykjavik",     ( 64.1466, -21.9426)),
    ("warsaw",        ( 52.2297,  21.0122)),
    ("prague",        ( 50.0755,  14.4378)),
    ("budapest",      ( 47.4979,  19.0402)),
    # Mediterranean / Middle East / Türkiye
    ("istanbul",      ( 41.0082,  28.9784)),
    ("tel aviv",      ( 32.0853,  34.7818)),
    ("jerusalem",     ( 31.7683,  35.2137)),
    ("dubai",         ( 25.2048,  55.2708)),
    ("abu dhabi",     ( 24.4539,  54.3773)),
    ("doha",          ( 25.2854,  51.5310)),
    ("riyadh",        ( 24.7136,  46.6753)),
    # North America — USA
    ("new york",      ( 40.7128, -74.0060)),
    ("brooklyn",      ( 40.6782, -73.9442)),
    ("boston",        ( 42.3601, -71.0589)),
    ("cambridge, ma", ( 42.3736, -71.1097)),
    ("cambridge, massachusetts", ( 42.3736, -71.1097)),
    ("providence",    ( 41.8240, -71.4128)),
    ("philadelphia",  ( 39.9526, -75.1652)),
    ("baltimore",     ( 39.2904, -76.6122)),
    ("washington, dc",( 38.9072, -77.0369)),
    ("washington dc", ( 38.9072, -77.0369)),
    ("pittsburgh",    ( 40.4406, -79.9959)),
    ("atlanta",       ( 33.7490, -84.3880)),
    ("nashville",     ( 36.1627, -86.7816)),
    ("orlando",       ( 28.5383, -81.3792)),
    ("miami",         ( 25.7617, -80.1918)),
    ("chicago",       ( 41.8781, -87.6298)),
    ("ann arbor",     ( 42.2808, -83.7430)),
    ("madison",       ( 43.0731, -89.4012)),
    ("minneapolis",   ( 44.9778, -93.2650)),
    ("st. louis",     ( 38.6270, -90.1994)),
    ("austin",        ( 30.2672, -97.7431)),
    ("dallas",        ( 32.7767, -96.7970)),
    ("houston",       ( 29.7604, -95.3698)),
    ("new orleans",   ( 29.9511, -90.0715)),
    ("denver",        ( 39.7392, -104.9903)),
    ("salt lake city",( 40.7608, -111.8910)),
    ("phoenix",       ( 33.4484, -112.0740)),
    ("las vegas",     ( 36.1699, -115.1398)),
    ("san diego",     ( 32.7157, -117.1611)),
    ("los angeles",   ( 34.0522, -118.2437)),
    ("pasadena",      ( 34.1478, -118.1445)),
    ("santa barbara", ( 34.4208, -119.6982)),
    ("san francisco", ( 37.7749, -122.4194)),
    ("berkeley",      ( 37.8716, -122.2727)),
    ("palo alto",     ( 37.4419, -122.1430)),
    ("santa clara",   ( 37.3541, -121.9552)),
    ("san jose",      ( 37.3382, -121.8863)),
    ("seattle",       ( 47.6062, -122.3321)),
    ("portland",      ( 45.5152, -122.6784)),
    ("honolulu",      ( 21.3069, -157.8583)),
    ("hawaii",        ( 19.8968, -155.5828)),
    # Canada
    ("toronto",       ( 43.6532, -79.3832)),
    ("ottawa",        ( 45.4215, -75.6972)),
    ("montreal",      ( 45.5017, -73.5673)),
    ("vancouver",     ( 49.2827, -123.1207)),
    ("calgary",       ( 51.0447, -114.0719)),
    ("edmonton",      ( 53.5461, -113.4938)),
    ("quebec city",   ( 46.8139, -71.2080)),
    ("halifax",       ( 44.6488, -63.5752)),
    ("waterloo",      ( 43.4643, -80.5204)),
    # Latin America
    ("mexico city",   ( 19.4326, -99.1332)),
    ("sao paulo",     (-23.5505, -46.6333)),
    ("são paulo",(-23.5505, -46.6333)),
    ("rio de janeiro",(-22.9068, -43.1729)),
    ("buenos aires",  (-34.6037, -58.3816)),
    ("santiago",      (-33.4489, -70.6693)),
    ("lima",          (-12.0464, -77.0428)),
    ("bogota",        (  4.7110, -74.0721)),
    # Asia — East
    ("tokyo",         ( 35.6762, 139.6503)),
    ("yokohama",      ( 35.4437, 139.6380)),
    ("kyoto",         ( 35.0116, 135.7681)),
    ("osaka",         ( 34.6937, 135.5023)),
    ("sapporo",       ( 43.0618, 141.3545)),
    ("seoul",         ( 37.5665, 126.9780)),
    ("busan",         ( 35.1796, 129.0756)),
    ("daejeon",       ( 36.3504, 127.3845)),
    ("beijing",       ( 39.9042, 116.4074)),
    ("shanghai",      ( 31.2304, 121.4737)),
    ("hong kong",     ( 22.3193, 114.1694)),
    ("shenzhen",      ( 22.5431, 114.0579)),
    ("guangzhou",     ( 23.1291, 113.2644)),
    ("chengdu",       ( 30.5728, 104.0668)),
    ("xi'an",         ( 34.3416, 108.9398)),
    ("hangzhou",      ( 30.2741, 120.1551)),
    ("nanjing",       ( 32.0603, 118.7969)),
    ("wuhan",         ( 30.5928, 114.3055)),
    ("taipei",        ( 25.0330, 121.5654)),
    # Asia — South/Southeast
    ("singapore",     (  1.3521, 103.8198)),
    ("kuala lumpur",  (  3.1390, 101.6869)),
    ("bangkok",       ( 13.7563, 100.5018)),
    ("phuket",        (  7.9519,  98.3381)),
    ("hanoi",         ( 21.0285, 105.8542)),
    ("ho chi minh",   ( 10.7626, 106.6602)),
    ("jakarta",       ( -6.2088, 106.8456)),
    ("manila",        ( 14.5995, 120.9842)),
    ("delhi",         ( 28.6139,  77.2090)),
    ("mumbai",        ( 19.0760,  72.8777)),
    ("bangalore",     ( 12.9716,  77.5946)),
    ("hyderabad",     ( 17.3850,  78.4867)),
    ("chennai",       ( 13.0827,  80.2707)),
    ("kolkata",       ( 22.5726,  88.3639)),
    # Oceania
    ("sydney",        (-33.8688, 151.2093)),
    ("melbourne",     (-37.8136, 144.9631)),
    ("brisbane",      (-27.4698, 153.0251)),
    ("perth",         (-31.9523, 115.8613)),
    ("adelaide",      (-34.9285, 138.6007)),
    ("canberra",      (-35.2809, 149.1300)),
    ("auckland",      (-36.8485, 174.7633)),
    ("wellington",    (-41.2865, 174.7762)),
    # Africa
    ("cape town",     (-33.9249,  18.4241)),
    ("johannesburg",  (-26.2041,  28.0473)),
    ("nairobi",       ( -1.2921,  36.8219)),
    ("cairo",         ( 30.0444,  31.2357)),
    ("lagos",         (  6.5244,   3.3792)),
    # Country-level fallbacks (last resort — very rough centroids)
    ("germany",       ( 51.1657,  10.4515)),
    ("france",        ( 46.2276,   2.2137)),
    ("uk",            ( 54.0000,  -2.0000)),
    ("united kingdom",( 54.0000,  -2.0000)),
    ("united states", ( 39.0000, -100.0000)),
    ("usa",           ( 39.0000, -100.0000)),
    ("canada",        ( 56.0000, -106.0000)),
    ("japan",         ( 36.2048, 138.2529)),
    ("china",         ( 35.8617, 104.1954)),
    ("india",         ( 20.5937,  78.9629)),
    ("australia",     (-25.2744, 133.7751)),
    ("brazil",        (-14.2350, -51.9253)),
]


def _lookup(location: str) -> tuple[float, float] | None:
    if not location:
        return None
    needle = location.lower()
    for substr, coords in CITIES:
        if substr in needle:
            return coords
    return None


def assign_coordinates() -> dict[str, int]:
    """Fill latitude/longitude for any row whose `location` matches a city in
    the static dict. Existing non-null coords are never overwritten."""
    matched = 0
    skipped_existing = 0
    skipped_no_match = 0
    with SessionLocal() as db:
        for row in db.query(Conference).all():
            if row.latitude is not None and row.longitude is not None:
                skipped_existing += 1
                continue
            if not row.location:
                continue
            coords = _lookup(row.location)
            if coords is None:
                skipped_no_match += 1
                continue
            row.latitude, row.longitude = coords
            matched += 1
        db.commit()
    return {
        "matched": matched,
        "skipped_existing": skipped_existing,
        "skipped_no_match": skipped_no_match,
    }
