import pytest

from mysql_ch_replicator.clickhouse_api import ClickhouseApi
from mysql_ch_replicator.config import (
    ClickhouseSettings,
    PartitionBy,
    Settings,
    partition_by_applies,
    referenced_columns,
)
from mysql_ch_replicator.table_structure import TableStructure, TableField


def make_structure(fields, primary_keys):
    structure = TableStructure(table_name='t')
    structure.fields = [TableField(name=name, field_type=type_) for name, type_ in fields]
    structure.primary_keys = primary_keys
    structure.preprocess()
    return structure


def make_api():
    """A ClickhouseApi without a connection, capturing the generated DDL."""
    api = object.__new__(ClickhouseApi)
    api.database = 'db'
    api.clickhouse_settings = ClickhouseSettings()
    api.captured = []
    api.execute_command = api.captured.append
    return api


@pytest.mark.parametrize('expression,expected', [
    ('toYYYYMM(created)', ['created']),
    ('toYYYYMM(toDateTime(created))', ['created']),
    ("toYYYYMM(created, 'UTC')", ['created']),
    ('tuple()', []),
    ('intDiv(id, 4294967)', ['id']),
    ('cityHash64(id) % 1000', ['id']),
    ('`created`', ['created']),
])
def test_referenced_columns(expression, expected):
    assert referenced_columns(expression) == expected


def test_partition_by_applies_to_existing_non_nullable_column():
    structure = make_structure(
        [('created', 'DateTime64(3)'), ('id', 'Int64')], ['id'],
    )

    assert partition_by_applies('toYYYYMM(created)', structure)


def test_partition_by_skipped_when_column_is_missing():
    """A catch-all rule must not break tables that lack the column."""
    structure = make_structure([('id', 'Int64')], ['id'])

    assert not partition_by_applies('toYYYYMM(created)', structure)


def test_partition_by_skipped_when_column_is_nullable():
    """ClickHouse rejects nullable partition keys."""
    structure = make_structure(
        [('created', 'Nullable(DateTime64(3))'), ('id', 'Int64')], ['id'],
    )

    assert not partition_by_applies('toYYYYMM(created)', structure)


def test_partition_by_without_columns_applies_to_any_table():
    structure = make_structure([('id', 'Int64')], ['id'])

    assert partition_by_applies('tuple()', structure)


def test_partition_by_applies_without_structure():
    assert partition_by_applies('toYYYYMM(anything)', None)


def make_settings(*partition_bys):
    settings = Settings()
    settings.partition_bys = [PartitionBy(**p) for p in partition_bys]
    return settings


def test_global_rule_is_used_for_tables_that_have_the_column():
    settings = make_settings(
        {'databases': '*', 'tables': '*', 'partition_by': 'toYYYYMM(created)'},
    )
    structure = make_structure(
        [('created', 'DateTime64(3)'), ('id', 'Int64')], ['id'],
    )

    assert settings.get_partition_bys('db', 't', structure)[0] == 'toYYYYMM(created)'


def test_global_rule_is_skipped_for_tables_without_the_column():
    settings = make_settings(
        {'databases': '*', 'tables': '*', 'partition_by': 'toYYYYMM(created)'},
    )
    structure = make_structure([('id', 'Int64')], ['id'])

    assert settings.get_partition_bys('db', 't', structure) == []


def test_first_applicable_rule_wins():
    """
    All matching rules are returned in config order and create_table uses the
    first one, so the most specific rule must be declared first.
    """
    settings = make_settings(
        {'databases': '*', 'tables': ['special_*'], 'partition_by': 'toYYYYMM(bus_time)'},
        {'databases': '*', 'tables': '*', 'partition_by': 'toYYYYMM(created)'},
    )
    with_bus_time = make_structure(
        [('bus_time', 'DateTime64(3)'), ('created', 'DateTime64(3)'), ('id', 'Int64')], ['id'],
    )
    without_bus_time = make_structure(
        [('created', 'DateTime64(3)'), ('id', 'Int64')], ['id'],
    )

    # The specific rule comes first, so it wins.
    assert settings.get_partition_bys('db', 'special_1', with_bus_time)[0] == 'toYYYYMM(bus_time)'
    # When its column is missing the rule is skipped and the next one applies.
    assert settings.get_partition_bys('db', 'special_1', without_bus_time)[0] == 'toYYYYMM(created)'


@pytest.mark.parametrize('primary_key_type', ['Int32', 'Int64', 'UInt64', 'String'])
def test_tables_are_unpartitioned_by_default(primary_key_type):
    """
    There is no default partition key. A 64-bit snowflake key would get one
    partition per row with intDiv(pk, 4294967) and ClickHouse would reject the
    INSERT with TOO_MANY_PARTS, and no automatic key suits every table, so a
    table is partitioned only when `partition_bys` asks for it.
    """
    api = make_api()
    structure = make_structure(
        [('id', primary_key_type), ('name', 'String')], ['id'],
    )

    api.create_table(structure)

    assert 'PARTITION BY' not in api.captured[0]


def test_configured_partition_by_is_used():
    api = make_api()
    structure = make_structure([('id', 'Int64')], ['id'])

    api.create_table(structure, additional_partition_bys=['toYYYYMM(created)'])

    assert 'PARTITION BY toYYYYMM(created)' in api.captured[0]


def test_composite_primary_key_is_unpartitioned_by_default():
    api = make_api()
    structure = make_structure(
        [('a', 'Int32'), ('b', 'Int32'), ('name', 'String')], ['a', 'b'],
    )

    api.create_table(structure)

    assert 'PARTITION BY' not in api.captured[0]
