"""Builds a small but valid GTFS zip for tests.

Deliberately not a copy of the real 8.4 MB feed: tests stay fast and run offline.
The contents are chosen to cover the cases that broke assumptions in the real
feed, above all a stop time past midnight and distances expressed in miles.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

AGENCY = """agency_id,agency_name,agency_url,agency_timezone,agency_lang,agency_phone
MTS,MTS,http://www.sdmts.com,America/Los_Angeles,en,619-233-3004
"""

ROUTES = """route_id,agency_id,route_short_name,route_long_name,route_type,route_color,route_text_color
1,MTS,1,Fashion Valley - La Mesa,3,000099,FFFFFF
510,MTS,510,Blue Line,0,0000FF,FFFFFF
"""

# service_weekday runs Mon-Fri, service_sunday runs Sundays only.
CALENDAR = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
service_weekday,1,1,1,1,1,0,0,20260907,20260930
service_sunday,0,0,0,0,0,0,1,20260907,20260930
"""

# 20260917 is a Thursday, removed from the weekday service.
# 20260919 is a Saturday, added to the sunday service.
CALENDAR_DATES = """service_id,date,exception_type
service_weekday,20260917,2
service_sunday,20260919,1
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id
1,service_weekday,trip_day,La Mesa,0,900102,shape_1
1,service_weekday,trip_owl,La Mesa,0,900103,shape_1
510,service_sunday,trip_sun,UTC,1,900200,shape_2
"""

# trip_owl crosses midnight: 25:30:00 and 26:05:00 are valid GTFS and cannot be
# represented as a time of day.
STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,pickup_type,drop_off_type,shape_dist_traveled,timepoint
trip_day,05:05:00,05:05:00,stop_a,1,,0,0,0.000000,1
trip_day,05:12:30,05:13:00,stop_b,2,,0,0,1.500000,0
trip_day,05:20:00,05:20:00,stop_c,3,,0,0,3.250000,1
trip_owl,25:30:00,25:30:00,stop_a,1,,0,0,0.000000,1
trip_owl,26:05:00,26:05:00,stop_c,2,,0,0,3.250000,1
trip_sun,12:00:00,12:00:00,stop_a,1,,0,0,0.000000,1
trip_sun,,,stop_b,2,,1,1,1.500000,0
"""

STOPS = """stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station,wheelchair_boarding
stop_a,1001,First & Main,32.71570000,-117.17000000,0,,1
stop_b,1002,Second & Main,32.72700000,-117.16900000,0,station_x,1
stop_c,1003,Third & Main,32.75450000,-117.19750000,0,,0
station_x,,Main Street Station,32.72700000,-117.16900000,1,,1
"""

SHAPES = """shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled
shape_1,32.71570000,-117.17000000,10001,0.000000
shape_1,32.72700000,-117.16900000,10002,1.500000
shape_1,32.75450000,-117.19750000,10003,3.250000
shape_2,32.75450000,-117.19750000,20001,0.000000
shape_2,32.87000000,-117.22000000,20002,8.400000
"""

FEED_INFO = """feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version
MTS,https://www.sdmts.com,EN,20260907,20260930,Generated on 20260901 @ 1200000
"""

# Present in the real feed and deliberately not loaded.
TRANSFERS = """from_stop_id,to_stop_id,transfer_type
stop_a,stop_b,0
"""

FILES = {
    "agency.txt": AGENCY,
    "routes.txt": ROUTES,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": CALENDAR_DATES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
    "stops.txt": STOPS,
    "shapes.txt": SHAPES,
    "feed_info.txt": FEED_INFO,
    "transfers.txt": TRANSFERS,
}

# Row counts the tests assert against, so a change to the fixture cannot quietly
# invalidate a test that checks loaded counts.
EXPECTED_ROWS = {
    "agencies": 1,
    "routes": 2,
    "stops": 4,
    "trips": 3,
    "stop_times": 7,
    "shapes": 5,
    "calendar": 2,
    "calendar_dates": 2,
}


def write_feed(
    path: Path, *, files: dict[str, str] | None = None, omit: tuple[str, ...] = ()
) -> Path:
    """Write a GTFS zip. omit drops files, for testing validation failures."""
    contents = dict(files or FILES)
    for name in omit:
        contents.pop(name, None)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in contents.items():
            archive.writestr(name, body)
    return path
