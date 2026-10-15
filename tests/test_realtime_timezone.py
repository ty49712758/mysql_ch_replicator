import datetime
import zoneinfo

import pytest

from mysql_ch_replicator.converter import MysqlToClickhouseConverter
from mysql_ch_replicator.pymysqlreplication.row_event import RowsEvent
from mysql_ch_replicator.table_structure import TableStructure, TableField


SHANGHAI = zoneinfo.ZoneInfo('Asia/Shanghai')
WALL_CLOCK = datetime.datetime(2026, 10, 15, 14, 23, 50)


class FakeSettings:
    def __init__(self, mysql_timezone):
        self.mysql_timezone = mysql_timezone


class FakeDbReplicator:
    def __init__(self, mysql_timezone):
        self.config = FakeSettings(mysql_timezone)


def convert_value(mysql_type, clickhouse_type, value, mysql_timezone='Asia/Shanghai'):
    """
    Run a single value through the same code path used for both initial and
    realtime replication.
    """
    converter = MysqlToClickhouseConverter()
    converter.db_replicator = FakeDbReplicator(mysql_timezone)

    mysql_structure = TableStructure(table_name='t')
    mysql_structure.fields = [
        TableField(name='id', field_type='int(11)'),
        TableField(name='ts', field_type=mysql_type),
    ]
    clickhouse_structure = TableStructure(table_name='t')
    clickhouse_structure.fields = [
        TableField(name='id', field_type='Int32'),
        TableField(name='ts', field_type=clickhouse_type),
    ]

    records = converter.convert_records(
        [(1, value)], mysql_structure, clickhouse_structure,
    )
    return records[0][1]


def test_naive_realtime_datetime_is_localized_to_mysql_timezone():
    """
    Realtime (binlog) rows carry naive datetimes. They must be localized to
    mysql_timezone, otherwise clickhouse-connect computes the epoch from the
    local timezone of the host process and every value is shifted by the
    difference between the host and mysql_timezone.
    """
    result = convert_value('datetime', 'DateTime64(3)', WALL_CLOCK)

    assert result.tzinfo == SHANGHAI
    assert result.replace(tzinfo=None) == WALL_CLOCK


def test_naive_realtime_datetime_epoch_does_not_depend_on_host_timezone():
    """
    The whole point of localizing the value: the instant that ends up in
    ClickHouse must be the wall clock read in mysql_timezone, no matter which
    timezone the replicator process happens to run in (e.g. a UTC container).
    """
    result = convert_value('datetime', 'DateTime64(3)', WALL_CLOCK)

    expected_epoch = WALL_CLOCK.replace(tzinfo=SHANGHAI).timestamp()
    assert result.timestamp() == expected_epoch


def test_naive_realtime_datetime_with_utc_config():
    result = convert_value('datetime', 'DateTime64(3)', WALL_CLOCK, mysql_timezone='UTC')

    assert result == WALL_CLOCK.replace(tzinfo=zoneinfo.ZoneInfo('UTC'))


def test_already_aware_initial_replication_value_is_left_untouched():
    """
    The initial replication reads values with a MySQL session time_zone applied
    and attaches mysql_timezone itself, so they are already aware and must not
    be converted a second time.
    """
    aware = WALL_CLOCK.replace(tzinfo=SHANGHAI)

    result = convert_value('datetime', 'DateTime64(3)', aware)

    assert result is aware


def test_non_datetime_values_are_not_touched():
    result = convert_value('int(11)', 'Int32', 42, mysql_timezone='Asia/Shanghai')

    assert result == 42


def make_rows_event(mysql_timezone):
    """
    A RowsEvent without running __init__, just enough to call the timestamp
    conversion used while decoding the binlog.
    """
    event = RowsEvent.__new__(RowsEvent)
    event.mysql_timezone = mysql_timezone
    return event


def test_binlog_timestamp_epoch_is_converted_to_mysql_timezone():
    """
    MySQL stores TIMESTAMP columns as a UTC epoch. The binlog carries that
    epoch, so it must be converted to mysql_timezone to match what the initial
    replication reads back.
    """
    event = make_rows_event('Asia/Shanghai')
    epoch = int(WALL_CLOCK.replace(tzinfo=SHANGHAI).timestamp())

    result = event._convert_timestamp_with_timezone(epoch)

    assert result == WALL_CLOCK.replace(tzinfo=SHANGHAI)


def test_binlog_timestamp_epoch_with_utc_config():
    event = make_rows_event('UTC')
    epoch = 1792045430

    result = event._convert_timestamp_with_timezone(epoch)

    assert result == datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)


def test_binlog_timestamp_falls_back_to_utc_on_unknown_timezone():
    event = make_rows_event('Not/AZone')
    epoch = 1792045430

    result = event._convert_timestamp_with_timezone(epoch)

    assert result == datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
