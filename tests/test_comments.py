import pytest

from clickhouse_connect.driver.exceptions import DatabaseError

from mysql_ch_replicator.clickhouse_api import (
    ClickhouseApi,
    ClickhouseSettings,
    comment_clause,
    escape_clickhouse_string,
    is_idempotent_ddl_error,
)
from mysql_ch_replicator.converter import (
    MysqlToClickhouseConverter,
    extract_comment,
    extract_create_table_comments,
)
from mysql_ch_replicator.table_structure import TableField, TableStructure


def test_extracts_column_and_table_comments():
    statement = (
        "CREATE TABLE `t` (\n"
        "  `id` bigint(20) NOT NULL COMMENT '主键id',\n"
        "  `name` varchar(100) NOT NULL COMMENT '商品名称',\n"
        "  PRIMARY KEY (`id`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存流水表'"
    )

    column_comments, table_comment = extract_create_table_comments(statement)

    assert column_comments == {'id': '主键id', 'name': '商品名称'}
    assert table_comment == '库存流水表'


def test_comment_may_contain_commas_and_parentheses():
    statement = (
        "CREATE TABLE `t` (\n"
        "  `a` varchar(10) COMMENT '第一列, 第二列 (备注)',\n"
        "  `b` int COMMENT 'x'\n"
        ") ENGINE=InnoDB"
    )

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments == {'a': '第一列, 第二列 (备注)', 'b': 'x'}


@pytest.mark.parametrize('literal,expected', [
    ("'it''s'", "it's"),          # doubled quote
    (r"'it\'s'", "it's"),         # backslash escaped
    (r"'a\\b'", 'a\\b'),
    ("'line\\nbreak'", 'line\nbreak'),
])
def test_quote_styles_are_unescaped(literal, expected):
    statement = f"CREATE TABLE `t` (`a` int COMMENT {literal}) ENGINE=InnoDB"

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments == {'a': expected}


def test_comment_is_not_confused_with_columns_named_comment():
    statement = (
        "CREATE TABLE `t` (\n"
        "  `comment` varchar(10) COMMENT '列名叫 comment',\n"
        "  `b` int\n"
        ") ENGINE=InnoDB COMMENT='表'"
    )

    column_comments, table_comment = extract_create_table_comments(statement)

    assert column_comments == {'comment': '列名叫 comment'}
    assert table_comment == '表'


def test_text_containing_the_word_comment_is_kept():
    """A COMMENT clause value that itself contains 'comment' must survive."""
    statement = "CREATE TABLE `t` (`a` int COMMENT 'a comment about comments') ENGINE=InnoDB"

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments == {'a': 'a comment about comments'}


def test_no_comments():
    statement = "CREATE TABLE `t` (`a` int, `b` varchar(10)) ENGINE=InnoDB"

    assert extract_create_table_comments(statement) == ({}, '')


def test_unquoted_column_names():
    statement = "CREATE TABLE t (id int COMMENT 'PK', name varchar(10) COMMENT 'N') ENGINE=InnoDB"

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments == {'id': 'PK', 'name': 'N'}


def test_enum_values_do_not_confuse_the_scanner():
    statement = (
        "CREATE TABLE `t` (\n"
        "  `state` enum('a, b','c') NOT NULL COMMENT '状态, 可选值',\n"
        "  KEY `idx` (`state`)\n"
        ") ENGINE=InnoDB"
    )

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments == {'state': '状态, 可选值'}


def test_index_comment_does_not_shadow_a_column():
    statement = (
        "CREATE TABLE `t` (\n"
        "  `id` int NOT NULL COMMENT '主键',\n"
        "  KEY `idx_id` (`id`) COMMENT '索引注释'\n"
        ") ENGINE=InnoDB"
    )

    column_comments, _ = extract_create_table_comments(statement)

    assert column_comments['id'] == '主键'
    assert 'idx_id' not in column_comments


# --- the comments must survive parsing and reach the ClickHouse DDL ------------


class FakeConfig:
    types_mapping = {}
    mysql_timezone = 'UTC'


class FakeDbReplicator:
    config = FakeConfig()

    def get_target_table_name(self, table_name):
        return table_name


def make_converter():
    converter = MysqlToClickhouseConverter()
    converter.db_replicator = FakeDbReplicator()
    return converter


def parse(statement):
    converter = make_converter()
    mysql_structure = converter.parse_mysql_table_structure(statement, required_table_name='t')
    return converter, mysql_structure, converter.convert_table_structure(mysql_structure)


def make_api():
    api = object.__new__(ClickhouseApi)
    api.database = 'db'
    api.clickhouse_settings = ClickhouseSettings()
    api.captured = []
    api.execute_command = api.captured.append
    return api


STATEMENT = (
    "CREATE TABLE `t` (\n"
    "  `id` bigint(20) NOT NULL COMMENT '主键id',\n"
    "  `name` varchar(255) DEFAULT NULL COMMENT '名称',\n"
    "  `plain` int DEFAULT NULL,\n"
    "  PRIMARY KEY (`id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存流水表'"
)


def test_parsed_structures_carry_comments():
    _, mysql_structure, clickhouse_structure = parse(STATEMENT)

    assert mysql_structure.comment == '库存流水表'
    assert clickhouse_structure.comment == '库存流水表'
    assert [f.comment for f in clickhouse_structure.fields] == ['主键id', '名称', '']


def test_created_table_carries_comments():
    _, _, clickhouse_structure = parse(STATEMENT)
    api = make_api()

    api.create_table(clickhouse_structure)

    ddl = api.captured[0]
    assert "`id` Int64 COMMENT '主键id'" in ddl
    assert "`name` Nullable(String) COMMENT '名称'" in ddl
    assert '`plain` Nullable(Int32)' in ddl
    assert 'COMMENT' not in ddl.split('`plain` Nullable(Int32)')[1].split(',')[0]
    assert ddl.rstrip().endswith("COMMENT '库存流水表'")
    # the table comment must come after SETTINGS, where ClickHouse expects it
    assert ddl.index('SETTINGS') < ddl.index("COMMENT '库存流水表'")


def test_no_comment_clause_when_mysql_has_none():
    _, _, clickhouse_structure = parse(
        "CREATE TABLE `t` (`id` int NOT NULL, PRIMARY KEY (`id`)) ENGINE=InnoDB",
    )
    api = make_api()

    api.create_table(clickhouse_structure)

    assert 'COMMENT' not in api.captured[0]


@pytest.mark.parametrize('value,expected', [
    ("it's", "it\\'s"),
    ('back\\slash', 'back\\\\slash'),
])
def test_clickhouse_string_escaping(value, expected):
    assert escape_clickhouse_string(value) == expected


def test_comment_clause():
    assert comment_clause('') == ''
    assert comment_clause("O'Brien") == " COMMENT 'O\\'Brien'"


# --- ALTER TABLE ------------------------------------------------------------


@pytest.mark.parametrize('statement,expected', [
    ("`a` Int32 COMMENT 'x'", 'x'),
    ("`a` Int32 NOT NULL COMMENT '带 空格 的注释'", '带 空格 的注释'),
    ("`a` Int32 COMMENT 'it''s'", "it's"),
    ("`a` Int32 COMMENT 'a comment about comments'", 'a comment about comments'),
    ("`a` Int32 NOT NULL", ''),
    ("`a` COMMENT varchar(10) COMMENT 'x'", 'x'),
])
def test_extract_comment(statement, expected):
    assert extract_comment(statement) == expected


class FakeAlterConfig:
    types_mapping = {}

    def is_database_matches(self, name):
        return True

    def is_table_matches(self, name):
        return True


class FakeClickhouseApi:
    def __init__(self):
        self.queries = []

    def get_on_cluster_clause(self):
        return ''

    def execute_command(self, query):
        self.queries.append(query)

    # ALTER handlers go through execute_ddl_command; record it like
    # execute_command so the generated DDL can be asserted.
    execute_ddl_command = execute_command


class FakeState:
    def __init__(self, structures):
        self.tables_structure = structures

    def save(self):
        pass


class FakeAlterDbReplicator:
    def __init__(self, mysql_structure, ch_structure):
        self.database = 'db'
        self.target_database = 'db'
        self.config = FakeAlterConfig()
        self.state = FakeState({'t': (mysql_structure, ch_structure)})
        self.clickhouse_api = FakeClickhouseApi()

    def get_target_table_name(self, name):
        return name


def run_alter(statement, mysql_fields, ch_fields):
    def build(fields):
        structure = TableStructure(table_name='t')
        structure.fields = [TableField(name=n, field_type=t) for n, t in fields]
        structure.primary_keys = ['id']
        structure.preprocess()
        return structure

    mysql_structure, ch_structure = build(mysql_fields), build(ch_fields)
    replicator = FakeAlterDbReplicator(mysql_structure, ch_structure)
    MysqlToClickhouseConverter(replicator).convert_alter_query(statement, 'db')
    return replicator, ch_structure


BASE_MYSQL = [('id', 'bigint')]
BASE_CH = [('id', 'Int64')]


def test_alter_add_column_carries_the_comment():
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` ADD COLUMN `c` varchar(20) NOT NULL COMMENT '新增列'",
        BASE_MYSQL, BASE_CH,
    )

    assert replicator.clickhouse_api.queries == [
        "ALTER TABLE `db`.`t`  ADD COLUMN IF NOT EXISTS `c` String COMMENT '新增列' AFTER id",
    ]
    assert ch_structure.get_field('c').comment == '新增列'


def test_alter_add_column_without_a_comment():
    replicator, _ = run_alter(
        "ALTER TABLE `t` ADD COLUMN `c` varchar(20)",
        BASE_MYSQL, BASE_CH,
    )

    assert 'COMMENT' not in replicator.clickhouse_api.queries[0]


def test_alter_add_column_comment_may_contain_spaces():
    replicator, _ = run_alter(
        "ALTER TABLE `t` ADD COLUMN `c` varchar(20) COMMENT '库存 id 备注'",
        BASE_MYSQL, BASE_CH,
    )

    assert "COMMENT '库存 id 备注'" in replicator.clickhouse_api.queries[0]


def test_alter_modify_column_puts_comment_after_default():
    """ClickHouse only accepts DEFAULT before COMMENT."""
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` MODIFY COLUMN `a` int NOT NULL COMMENT '注释'",
        BASE_MYSQL + [('a', 'int')], BASE_CH + [('a', 'Nullable(Int32)')],
    )

    query = replicator.clickhouse_api.queries[0]
    assert query.index('DEFAULT') < query.index("COMMENT '注释'")
    assert ch_structure.get_field('a').comment == '注释'


def test_alter_modify_without_comment_clears_it():
    """MySQL drops the comment when MODIFY leaves it out; mirror that."""
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` MODIFY COLUMN `a` int NOT NULL",
        BASE_MYSQL + [('a', 'int')], BASE_CH + [('a', 'Int32')],
    )

    assert replicator.clickhouse_api.queries[0].rstrip().endswith("COMMENT ''")
    assert ch_structure.get_field('a').comment == ''


def test_alter_change_column_comment_only_change_is_pushed():
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` CHANGE COLUMN `a` `a` int COMMENT '改过的注释'",
        BASE_MYSQL + [('a', 'int')], BASE_CH + [('a', 'Int32')],
    )

    assert (
        "MODIFY COLUMN a Nullable(Int32) COMMENT '改过的注释'"
        in replicator.clickhouse_api.queries[0]
    )
    assert ch_structure.get_field('a').comment == '改过的注释'


def test_a_comment_mentioning_not_null_does_not_change_nullability():
    """
    The comment text must not leak into the parameters, where 'not null' would
    be read as a NOT NULL marker and drop the Nullable wrapper.
    """
    replicator, _ = run_alter(
        "ALTER TABLE `t` MODIFY COLUMN `a` int COMMENT 'use not null carefully'",
        BASE_MYSQL + [('a', 'int')], BASE_CH + [('a', 'Int32')],
    )

    query = replicator.clickhouse_api.queries[0]
    assert 'Nullable(Int32)' in query
    assert "COMMENT 'use not null carefully'" in query


# --- replaying a DDL that was already applied must be a no-op ----------------


def test_replayed_add_column_uses_if_not_exists_and_keeps_one_field():
    """
    A crash can leave ClickHouse with the column while the binlog position is
    still behind it, so the ADD COLUMN is replayed. It must not fail nor add
    the field twice to the tracked structure.
    """
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` ADD COLUMN `c` varchar(20) AFTER id",
        BASE_MYSQL + [('c', 'varchar(20)')], BASE_CH + [('c', 'String')],
    )

    assert replicator.clickhouse_api.queries == [
        "ALTER TABLE `db`.`t`  ADD COLUMN IF NOT EXISTS `c` Nullable(String) AFTER id",
    ]
    assert [f.name for f in ch_structure.fields] == ['id', 'c']


def test_replayed_add_column_first_keeps_one_field():
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` ADD COLUMN `c` varchar(20) FIRST",
        BASE_MYSQL + [('c', 'varchar(20)')], BASE_CH + [('c', 'String')],
    )

    assert 'ADD COLUMN IF NOT EXISTS `c` Nullable(String) FIRST' in replicator.clickhouse_api.queries[0]
    # The column is already tracked, so it is left where it is rather than moved.
    assert [f.name for f in ch_structure.fields] == ['id', 'c']


def test_mysql_only_table_options_are_ignored():
    """
    gh-ost style ALTERs carry MySQL-only options as trailing clauses, e.g.
    "ALGORITHM=INPLACE, LOCK=SHARED". They must not be treated as an unknown
    operation.
    """
    statement = (
        "ALTER TABLE `t`\n"
        "    ADD COLUMN `c` int AFTER id,\n"
        "    ADD COLUMN `d` int AFTER `c`,\n"
        "    ALGORITHM=INPLACE, LOCK=SHARED"
    )
    replicator, ch_structure = run_alter(statement, BASE_MYSQL, BASE_CH)

    assert replicator.clickhouse_api.queries == [
        "ALTER TABLE `db`.`t`  ADD COLUMN IF NOT EXISTS `c` Nullable(Int32) AFTER id",
        "ALTER TABLE `db`.`t`  ADD COLUMN IF NOT EXISTS `d` Nullable(Int32) AFTER c",
    ]
    assert [f.name for f in ch_structure.fields] == ['id', 'c', 'd']


def test_replayed_drop_column_is_idempotent():
    """The column may already be gone when the DROP COLUMN is replayed."""
    replicator, ch_structure = run_alter(
        "ALTER TABLE `t` DROP COLUMN `c`",
        BASE_MYSQL, BASE_CH,
    )

    assert replicator.clickhouse_api.queries == [
        "ALTER TABLE `db`.`t`  DROP COLUMN IF EXISTS c",
    ]
    assert [f.name for f in ch_structure.fields] == ['id']


@pytest.mark.parametrize('code,name,message,expected', [
    (15, None, 'Code: 15. DB::Exception: Cannot add column `c`: column with this name already exists. (DUPLICATE_COLUMN)', True),
    (16, None, 'Code: 16. DB::Exception: There is no column `c`. (NO_SUCH_COLUMN_IN_TABLE)', True),
    (47, None, 'Code: 47. DB::Exception: Unknown identifier `c`. (UNKNOWN_IDENTIFIER)', True),
    (None, 'DUPLICATE_COLUMN', 'DB::Exception: column already exists', True),
    (None, None, 'Code: 15. DB::Exception: ... (DUPLICATE_COLUMN)', True),
    (60, None, 'Code: 60. DB::Exception: Table db.t does not exist. (UNKNOWN_TABLE)', False),
    (None, None, 'Code: 62. Syntax error', False),
])
def test_is_idempotent_ddl_error(code, name, message, expected):
    assert is_idempotent_ddl_error(DatabaseError(message, code=code, name=name)) is expected


def _ddl_api(monkeypatch, error):
    api = object.__new__(ClickhouseApi)
    api.captured = []

    def execute_command(query):
        api.captured.append(query)
        raise error

    api.execute_command = execute_command
    return api


def test_execute_ddl_command_swallows_duplicate_column(monkeypatch):
    api = _ddl_api(monkeypatch, DatabaseError(
        'Code: 15. DB::Exception: column with this name already exists. (DUPLICATE_COLUMN)',
        code=15,
    ))

    # must not raise
    api.execute_ddl_command('ALTER TABLE `db`.`t` ADD COLUMN `c` Int32')

    assert api.captured == ['ALTER TABLE `db`.`t` ADD COLUMN `c` Int32']


def test_execute_ddl_command_reraises_other_errors(monkeypatch):
    api = _ddl_api(monkeypatch, DatabaseError(
        'Code: 60. DB::Exception: Table db.t does not exist. (UNKNOWN_TABLE)',
        code=60,
    ))

    with pytest.raises(DatabaseError):
        api.execute_ddl_command('ALTER TABLE `db`.`t` ADD COLUMN `c` Int32')
