import datetime
import re
import zoneinfo

import pytest

from mysql.connector.errors import DatabaseError, get_mysql_exception

from mysql_ch_replicator.mysql_api import format_utc_offset, set_session_time_zone


class FakeCursor:
    """
    Cursor of a MySQL server whose time zone tables (mysql.time_zone*) are not
    populated: named time zones are rejected with error 1298, while numeric
    offsets are understood.
    """

    def __init__(self, fail_named_time_zones=True, errno=1298):
        self.queries = []
        self.fail_named_time_zones = fail_named_time_zones
        self.errno = errno

    def execute(self, query):
        match = re.search(r"SET time_zone = '([^']+)'", query)
        if match and self.fail_named_time_zones:
            time_zone = match.group(1)
            if not re.fullmatch(r'[+-]\d{2}:\d{2}', time_zone):
                raise get_mysql_exception(
                    self.errno, 'HY000',
                    f"Unknown or incorrect time zone: '{time_zone}'",
                )
        self.queries.append(query)


@pytest.mark.parametrize('offset,expected', [
    (datetime.timedelta(hours=8), '+08:00'),
    (datetime.timedelta(hours=-5, minutes=-30), '-05:30'),
    (datetime.timedelta(0), '+00:00'),
    (datetime.timedelta(hours=5, minutes=45), '+05:45'),
    (datetime.timedelta(hours=9, minutes=30), '+09:30'),
])
def test_format_utc_offset(offset, expected):
    assert format_utc_offset(offset) == expected


def test_uses_named_time_zone_when_server_knows_it():
    cursor = FakeCursor(fail_named_time_zones=False)

    set_session_time_zone(cursor, 'Asia/Shanghai')

    assert cursor.queries == ["SET time_zone = 'Asia/Shanghai'"]


def test_falls_back_to_numeric_offset_when_time_zone_tables_missing():
    """
    Reproduces the startup failure on MySQL servers that have no time zone
    tables loaded:

        DatabaseError: 1298 (HY000): Unknown or incorrect time zone: 'Asia/Shanghai'

    Instead of failing, the replicator should fall back to the numeric offset.
    """
    cursor = FakeCursor()

    set_session_time_zone(cursor, 'Asia/Shanghai')

    assert cursor.queries == ["SET time_zone = '+08:00'"]


def test_fallback_offset_equals_current_offset_of_the_zone():
    # Date independent: the fallback must be the zone's current UTC offset.
    cursor = FakeCursor()

    set_session_time_zone(cursor, 'America/New_York')

    expected = format_utc_offset(
        datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York')).utcoffset()
    )
    assert cursor.queries == [f"SET time_zone = '{expected}'"]


def test_other_database_errors_are_not_swallowed():
    cursor = FakeCursor(errno=1045)

    with pytest.raises(DatabaseError):
        set_session_time_zone(cursor, 'Asia/Shanghai')

    assert cursor.queries == []
