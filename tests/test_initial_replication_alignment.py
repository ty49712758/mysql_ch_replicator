from mysql_ch_replicator.db_replicator_initial import align_records_to_structure
from mysql_ch_replicator.table_structure import TableStructure, TableField


def make_structure(*field_names):
    structure = TableStructure()
    structure.fields = [TableField(name=name) for name in field_names]
    return structure


def test_no_structure_change_keeps_records_untouched():
    records = [(1, 'a'), (2, 'b')]

    aligned, missing, extra = align_records_to_structure(['id', 'name'], records, make_structure('id', 'name'))

    assert aligned == records
    assert missing == []
    assert extra == []


def test_added_column_is_ignored():
    # The copy started with (id, name); a DDL added `status` while it was running.
    records = [(1, 'a', 'active'), (2, 'b', 'inactive')]

    aligned, missing, extra = align_records_to_structure(
        ['id', 'name', 'status'], records, make_structure('id', 'name'),
    )

    assert aligned == [(1, 'a'), (2, 'b')]
    assert missing == []
    assert extra == ['status']


def test_dropped_column_is_filled_with_none():
    # The copy started with (id, name, status); `status` was dropped while it was running.
    records = [(1, 'a'), (2, 'b')]

    aligned, missing, extra = align_records_to_structure(
        ['id', 'name'], records, make_structure('id', 'name', 'status'),
    )

    assert aligned == [(1, 'a', None), (2, 'b', None)]
    assert missing == ['status']
    assert extra == []


def test_renamed_column_is_reported_as_missing_and_extra():
    # RENAME COLUMN name -> full_name: the initial replication treats this as
    # "missing + extra", which the caller rejects as an unsupported rename.
    records = [(1, 'a')]

    aligned, missing, extra = align_records_to_structure(
        ['id', 'full_name'], records, make_structure('id', 'name'),
    )

    assert missing == ['name']
    assert extra == ['full_name']
    assert aligned == [(1, None)]


def test_reordered_columns_are_matched_by_name():
    records = [(1, 'active', 'a'), (2, 'inactive', 'b')]

    aligned, missing, extra = align_records_to_structure(
        ['id', 'status', 'name'], records, make_structure('id', 'name', 'status'),
    )

    assert aligned == [(1, 'a', 'active'), (2, 'b', 'inactive')]
    assert missing == []
    assert extra == []
