import datetime
import time
import re
import clickhouse_connect

from logging import getLogger
from dataclasses import dataclass, field
from collections import defaultdict

from .config import ClickhouseSettings
from .table_structure import TableStructure, TableField


logger = getLogger(__name__)

# Table engines that do not support OPTIMIZE TABLE (e.g. View).
NON_OPTIMIZABLE_TABLE_ENGINES = frozenset({
    'View',
    'MaterializedView',
    'LiveView',
    'WindowView',
})


def is_optimizable_table_engine(engine: str) -> bool:
    return engine not in NON_OPTIMIZABLE_TABLE_ENGINES


# Incremental replication replays binlog events at-least-once (the position is
# only persisted once the event has been fully handled), so a repeat of a DDL
# that already succeeded must not kill the replicator - it is downgraded to a
# no-op instead.
#
# The tolerated errors are grouped by what the statement targets, so a statement
# only ignores the errors that are harmless for *its own* target: an ALTER on a
# column must not silently swallow a missing table. Each entry is
# (symbolic name, ClickHouse error code).
COLUMN_ALREADY_APPLIED_ERRORS = frozenset({
    ('DUPLICATE_COLUMN', 15),          # column with this name already exists
    ('NO_SUCH_COLUMN_IN_TABLE', 16),   # there is no column with this name
    ('UNKNOWN_IDENTIFIER', 47),        # identifier is not recognized (renamed away)
})

TABLE_ALREADY_APPLIED_ERRORS = frozenset({
    ('TABLE_ALREADY_EXISTS', 57),       # table with this name already exists
    ('UNKNOWN_TABLE', 60),              # table does not exist
})


def is_already_applied_ddl_error(error, tolerated_errors) -> bool:
    """True if `error` is one of `tolerated_errors` (i.e. the DDL was applied)."""
    code = getattr(error, 'code', None)
    name = getattr(error, 'name', None)
    # Older driver versions expose neither code nor name - fall back to the
    # symbolic name ClickHouse embeds in the message, e.g. "(DUPLICATE_COLUMN)".
    message = str(error)
    return any(
        code == error_code or name == error_name or error_name in message
        for error_name, error_code in tolerated_errors
    )


def escape_clickhouse_string(value: str) -> str:
    """Quote a value for a ClickHouse string literal."""
    return value.replace('\\', '\\\\').replace("'", "\\'")


def comment_clause(comment: str) -> str:
    """Render a MySQL COMMENT as a ClickHouse COMMENT clause, if there is one."""
    if not comment:
        return ''
    return f" COMMENT '{escape_clickhouse_string(comment)}'"


def modify_comment_clause(comment: str) -> str:
    """
    Render the COMMENT clause of `ALTER TABLE ... MODIFY/CHANGE COLUMN`.

    MySQL replaces the whole column definition, so a MODIFY that leaves COMMENT
    out drops the existing comment. ClickHouse keeps it instead, so an empty
    COMMENT is emitted explicitly to mirror MySQL. The clause must follow the
    DEFAULT clause, which is the only order ClickHouse accepts.
    """
    return f" COMMENT '{escape_clickhouse_string(comment)}'"


CREATE_TABLE_QUERY = '''
CREATE TABLE {if_not_exists} `{db_name}`.`{table_name}` {on_cluster}
(
{fields},
    `_version` UInt64,
    {indexes}
)
{engine}
{partition_by}ORDER BY {primary_key}
SETTINGS index_granularity = 8192{table_comment}
'''

CREATE_DIST_TABLE_QUERY = '''
CREATE TABLE {if_not_exists} `{db_name}`.`{dist_table_name}` {on_cluster}
ENGINE = Distributed('{cluster}', '{db_name}', '{table_name}', rand());
'''

DELETE_QUERY = '''
DELETE FROM `{db_name}`.`{table_name}` {on_cluster} WHERE ({field_name}) IN ({field_values})
'''


@dataclass
class SingleStats:
    duration: float = 0.0
    events: int = 0
    records: int = 0

    def to_dict(self):
        return self.__dict__


@dataclass
class InsertEraseStats:
    inserts: SingleStats = field(default_factory=SingleStats)
    erases: SingleStats = field(default_factory=SingleStats)

    def to_dict(self):
        return {
            'inserts': self.inserts.to_dict(),
            'erases': self.erases.to_dict(),
        }


@dataclass
class GeneralStats:
    general: InsertEraseStats = field(default_factory=InsertEraseStats)
    table_stats: dict[str, InsertEraseStats] = field(default_factory=lambda: defaultdict(InsertEraseStats))

    def on_event(self, table_name: str, is_insert: bool, duration: float, records: int):
        targets = []
        if is_insert:
            targets.append(self.general.inserts)
            targets.append(self.table_stats[table_name].inserts)
        else:
            targets.append(self.general.erases)
            targets.append(self.table_stats[table_name].erases)

        for target in targets:
            target.duration += duration
            target.events += 1
            target.records += records

    def to_dict(self):
        results = {'total': self.general.to_dict()}
        for table_name, table_stats in self.table_stats.items():
            results[table_name] = table_stats.to_dict()
        return results


class ClickhouseApi:
    MAX_RETRIES = 5
    RETRY_INTERVAL = 30
    DISTRIBUTED_TABLE_SUFFIX = '_distributed'

    def __init__(self, database: str | None, clickhouse_settings: ClickhouseSettings, version_initial_value: int = 0):
        self.database = database
        self.clickhouse_settings = clickhouse_settings
        self.erase_batch_size = clickhouse_settings.erase_batch_size
        self.version_initial_value = version_initial_value
        self.client = clickhouse_connect.get_client(
            host=clickhouse_settings.host,
            port=clickhouse_settings.port,
            username=clickhouse_settings.user,
            password=clickhouse_settings.password,
            connect_timeout=clickhouse_settings.connection_timeout,
            send_receive_timeout=clickhouse_settings.send_receive_timeout,
        )
        self.tables_last_record_version = {}
        self.stats = GeneralStats()
        self.execute_command('SET final = 1;')

    def get_stats(self):
        stats = self.stats.to_dict()
        self.stats = GeneralStats()
        return stats

    def get_tables(self):
        query = 'SHOW TABLES'
        # todo: https://github.com/bakwc/mysql_ch_replicator/issues/225
        #query = f'SHOW TABLES FROM `{self.database}`'
        result = self.client.query(query)
        tables = result.result_rows
        table_list = [row[0] for row in tables]
        return table_list

    def get_optimizable_tables(self, database: str) -> set[str]:
        escaped_db = database.replace("'", "''")
        result = self.client.query(
            f"SELECT name, engine FROM system.tables WHERE database = '{escaped_db}'",
        )
        return {
            row[0]
            for row in result.result_rows
            if is_optimizable_table_engine(row[1])
        }

    def get_table_structure(self, table_name):
        result = self.client.query(f'DESCRIBE TABLE `{self.database}`.`{table_name}`')
        structure = TableStructure()
        structure.table_name = table_name
        for row in result.result_rows:
            field_name = row[0]
            field_type = row[1]
            if field_name == '_version':
                continue
            structure.fields.append(TableField(
                name=field_name,
                field_type=field_type,
            ))
        
        create_statement = self.show_create_table(table_name)
        order_by_match = re.search(r'ORDER BY\s*\(([^)]+)\)', create_statement)
        if order_by_match:
            keys_str = order_by_match.group(1)
            structure.primary_keys = [k.strip().strip('`') for k in keys_str.split(',')]
        else:
            order_by_match = re.search(r'ORDER BY\s+(\S+)', create_statement)
            if order_by_match:
                structure.primary_keys = [order_by_match.group(1).strip('`')]
        
        structure.preprocess()
        return structure

    def get_databases(self):
        result = self.client.query('SHOW DATABASES')
        databases = result.result_rows
        database_list = [row[0] for row in databases]
        return database_list

    def execute_command(self, query):

        # helps run alter, rename, truncate & drop commands from other modules on cluster if cluster mode is enabled
        on_cluster = self.get_on_cluster_clause()
        query = self.apply_on_cluster(query, on_cluster) 

        if self.clickhouse_settings.cluster:
            # we are in cluster mode so we need to be careful with queries and run them in their desired context
            if query.startswith('USE') or query.startswith('SET') or query.startswith('SHOW') or query.startswith('SYSTEM') \
             or query.startswith('EXPLAIN') or query.startswith('CHECK') or query.startswith('DESCRIBE') or query.startswith('EXISTS') \
             or query.startswith('MOVE'):
                # nothing to be done here
                pass
            
            elif query.startswith('SELECT') and 'system.' not in query and self.DISTRIBUTED_TABLE_SUFFIX not in query:
                # if not selecting from system.* tables then we should be querying distributed table right?
                raise RuntimeError(f'SELECT in cluster mode should be done on distributed table: {query}')
            
            elif query.startswith('INSERT') and self.DISTRIBUTED_TABLE_SUFFIX not in query:
                # in cluster mode we should be inserting data to distubted table 
                raise RuntimeError(f'INSERT in cluster mode should be done on distributed table: {query}')

            elif query.startswith('CREATE') or query.startswith('ALTER') or query.startswith('DELETE') or query.startswith('GRANT') \
             or query.startswith('REVOKE') or query.startswith('UPDATE') or query.startswith('ATTACH') or query.startswith('DETACH') \
             or query.startswith('DROP') or query.startswith('UNDROP') or query.startswith('KILL') or query.startswith('OPTIMIZE') \
             or query.startswith('RENAME') or query.startswith('EXCHANGE') or query.startswith('TRUNCATE'):
                if 'ON CLUSTER' not in query:
                    raise NotImplementedError(f'This query should be run on cluster: {query}')
            else:
                raise RuntimeError(f'Not sure what to do with this query in cluster mode: {query}')


        for attempt in range(ClickhouseApi.MAX_RETRIES):
            try:                        
                self.client.command(query)
                break
            except clickhouse_connect.driver.exceptions.OperationalError as e:
                logger.error(f'error executing command {query}: {e}', exc_info=e)
                if attempt == ClickhouseApi.MAX_RETRIES - 1:
                    raise e
                time.sleep(ClickhouseApi.RETRY_INTERVAL)

    def execute_ddl_command(self, query, tolerated_errors=COLUMN_ALREADY_APPLIED_ERRORS):
        """Execute a DDL query that may be replayed by the replicator.

        A replayed DDL whose effect is already in place raises a ClickHouse
        DatabaseError (e.g. DUPLICATE_COLUMN). That is not a real failure, so it
        is logged and swallowed instead of crashing the replicator; any other
        error is re-raised. `tolerated_errors` selects which "already applied"
        errors are harmless for this statement's target - see
        COLUMN_ALREADY_APPLIED_ERRORS / TABLE_ALREADY_APPLIED_ERRORS.
        """
        try:
            self.execute_command(query)
        except clickhouse_connect.driver.exceptions.DatabaseError as e:
            if is_already_applied_ddl_error(e, tolerated_errors):
                logger.warning(f'skipping DDL, change already applied: {query} ({e})')
                return
            # ClickHouse does not name the target table in the error (e.g.
            # "Cannot add column `c`: column with this name already exists"), so
            # add the failing query back to make the log actionable.
            logger.error(f'error executing DDL: {query}: {e}')
            raise clickhouse_connect.driver.exceptions.DatabaseError(
                f'{e} [query: {query}]',
                code=getattr(e, 'code', None),
                name=getattr(e, 'name', None),
            ) from e

    def recreate_database(self):
        self.drop_database(self.database)
        self.create_database(self.database)

    def get_last_used_version(self, table_name):
        if table_name in self.tables_last_record_version:
            return self.tables_last_record_version[table_name]
        return self.version_initial_value

    def set_last_used_version(self, table_name, last_used_version):
        self.tables_last_record_version[table_name] = last_used_version

    def get_on_cluster_clause(self):
        if self.clickhouse_settings.cluster:
            return f'ON CLUSTER {self.clickhouse_settings.cluster}' 
        return ''

    def apply_on_cluster(self, query, on_cluster):
        query = query.format(**{'on_cluster': on_cluster}) if '{on_cluster}' in query else query
        return query.strip()

    def get_distributed_table_schema(self, table_name, database, if_not_exists=False): 
        # distributed table schema
        table_name = table_name.replace(self.DISTRIBUTED_TABLE_SUFFIX, '') if table_name.endswith(self.DISTRIBUTED_TABLE_SUFFIX) else table_name
        return CREATE_DIST_TABLE_QUERY.format(**{
            'if_not_exists': 'IF NOT EXISTS' if if_not_exists else '',
            'db_name': database,
            'table_name': table_name,
            'dist_table_name': table_name + self.DISTRIBUTED_TABLE_SUFFIX,
            'on_cluster': self.get_on_cluster_clause(),
            'cluster': self.clickhouse_settings.cluster

        })


    def create_table(self, structure: TableStructure, additional_indexes: list | None = None, additional_partition_bys: list | None = None, additional_order_bys: list | None = None):
        if not structure.primary_keys:
            raise Exception(f'missing primary key for {structure.table_name}')

        fields = [
            f'    `{field.name}` {field.field_type}{comment_clause(field.comment)}'
            for field in structure.fields
        ]
        fields = ',\n'.join(fields)

        # Tables are created without a partition key unless `partition_bys`
        # configures one. There is no sensible default: spreading a 64-bit key
        # such as a snowflake id with intDiv() would create one partition per
        # row (ClickHouse then rejects the INSERT with TOO_MANY_PARTS), and a key
        # that is not tied to the data volume cannot be chosen automatically. A
        # single unpartitioned part is always valid, so that is the default.
        partition_by = ''
        if additional_partition_bys:
            # Use the first matching custom partition_by
            partition_by = f'PARTITION BY {additional_partition_bys[0]}\n'

        indexes = [
            'INDEX _version _version TYPE minmax GRANULARITY 1',
        ]
        if len(structure.primary_keys) == 1:
            indexes.append(
                f'INDEX idx_id {structure.primary_keys[0]} TYPE bloom_filter GRANULARITY 1',
            )
        if additional_indexes is not None:
            indexes += additional_indexes

        indexes = ',\n'.join(indexes)
        
        # Check for custom order_by first
        if additional_order_bys:
            primary_key = additional_order_bys[0]
            if ',' in primary_key:
                primary_key = f'({primary_key})'
        else:
            primary_key = ','.join(structure.primary_keys)
            if len(structure.primary_keys) > 1:
                primary_key = f'({primary_key})'

        engine = 'ENGINE = ReplacingMergeTree(_version)'        
        on_cluster = self.get_on_cluster_clause()
        queries = list()

        if self.clickhouse_settings.cluster:
            # will fail if shard & replica macros aren't configured in clickhouse-server config
            engine = "ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{database}/{table}/{shard}', '{replica}', _version)"
            query = self.get_distributed_table_schema(structure.table_name, self.database, if_not_exists=True)
            queries.append(query)


        # Always IF NOT EXISTS: a CREATE TABLE event can be replayed (the binlog
        # position is only persisted once the event was fully handled), and the
        # table may already be there. In cluster mode this also repairs a crash
        # between creating the replicated and the distributed table.
        query = CREATE_TABLE_QUERY.format(**{
            'if_not_exists': 'IF NOT EXISTS',
            'db_name': self.database,
            'table_name': structure.table_name,
            'on_cluster': on_cluster,
            'fields': fields,
            'primary_key': primary_key,
            'engine': engine,
            'partition_by': partition_by,
            'indexes': indexes,
            'table_comment': comment_clause(structure.comment),
        })

        queries.insert(0, query) # in case of cluster mode we need to create Replicated Table first before Distributed one
        for query in queries:
            logger.debug(f'create table query: {query}')
            self.execute_ddl_command(query, TABLE_ALREADY_APPLIED_ERRORS)

    def insert(self, table_name, records, table_structure: TableStructure = None):
        current_version = self.get_last_used_version(table_name) + 1

        records_to_insert = []
        for record in records:
            new_record = []
            for i, e in enumerate(record):
                if isinstance(e, datetime.date) and not isinstance(e, datetime.datetime):
                    try:
                        e = datetime.datetime.combine(e, datetime.time())
                    except ValueError:
                        e = datetime.datetime(1970, 1, 1)
                if isinstance(e, datetime.datetime):
                    try:
                        e.timestamp()
                    except ValueError:
                        e = datetime.datetime(1970, 1, 1)
                if table_structure is not None:
                    field: TableField = table_structure.fields[i]
                    is_datetime = (
                        ('DateTime' in field.field_type) or
                        ('Date32' in field.field_type)
                    )
                    if is_datetime and 'Nullable' not in field.field_type:
                        try:
                            e.timestamp()
                        except (ValueError, AttributeError):
                            e = datetime.datetime(1970, 1, 1)
                new_record.append(e)
            record = new_record

            records_to_insert.append(tuple(record) + (current_version,))
            current_version += 1

        # in cluster mode we want to insert data to distributed so that its replicated among other shards & replicas
        if self.clickhouse_settings.cluster:
            table_name += self.DISTRIBUTED_TABLE_SUFFIX

        full_table_name = f'`{table_name}`'
        if '.' not in full_table_name:
            full_table_name = f'`{self.database}`.`{table_name}`'

        duration = 0.0
        for attempt in range(ClickhouseApi.MAX_RETRIES):
            try:
                t1 = time.time()
                self.client.insert(table=full_table_name, data=records_to_insert)
                t2 = time.time()
                duration += (t2 - t1)
                break
            except clickhouse_connect.driver.exceptions.OperationalError as e:
                logger.error(f'error inserting data: {e}', exc_info=e)
                if attempt == ClickhouseApi.MAX_RETRIES - 1:
                    raise e
                time.sleep(ClickhouseApi.RETRY_INTERVAL)

        self.stats.on_event(
            table_name=table_name,
            duration=duration,
            is_insert=True,
            records=len(records_to_insert),
        )

        self.set_last_used_version(table_name, current_version)

    def erase(self, table_name, field_name, field_values):
        field_name = ','.join(field_name)
        
        # Batch large deletions to avoid ClickHouse max query size limit
        field_values_list = list(field_values)
        
        total_duration = 0.0
        total_records = len(field_values_list)
        on_cluster = self.get_on_cluster_clause()

        for i in range(0, len(field_values_list), self.erase_batch_size):
            batch = field_values_list[i:i + self.erase_batch_size]
            batch_field_values = ', '.join(f'({v})' for v in batch)
            
            query = DELETE_QUERY.format(**{
                'db_name': self.database,
                'table_name': table_name,
                'on_cluster': on_cluster,
                'field_name': field_name,
                'field_values': batch_field_values,
            })
            
            t1 = time.time()
            self.execute_command(query)
            t2 = time.time()
            total_duration += (t2 - t1)
        
        self.stats.on_event(
            table_name=table_name,
            duration=total_duration,
            is_insert=False,
            records=total_records,
        )

    def drop_database(self, db_name):
        on_cluster = self.get_on_cluster_clause()
        self.execute_command(f'DROP DATABASE IF EXISTS `{db_name}` {on_cluster}')

    def create_database(self, db_name, if_not_exists=False):
        if_not_exists_clause = 'IF NOT EXISTS ' if if_not_exists else ''
        on_cluster = self.get_on_cluster_clause()
        self.execute_command(f'CREATE DATABASE {if_not_exists_clause}`{db_name}` {on_cluster}')

    def drop_table(self, table_name):
        on_cluster = self.get_on_cluster_clause()
        query = f'DROP TABLE IF EXISTS `{self.database}`.`{table_name}` {on_cluster}'
        logger.debug(query)
        self.execute_command(query)

    def select(self, table_name, where=None, final=None):
        query = f'SELECT * FROM {table_name}'
        if where:
            query += f' WHERE {where}'
        if final is not None:
            query += f' SETTINGS final = {int(final)};'
        result = self.client.query(query)
        rows = result.result_rows
        columns = result.column_names

        results = []
        for row in rows:
            results.append(dict(zip(columns, row)))
        return results

    def query(self, query: str):
        return self.client.query(query)

    def show_create_table(self, table_name):
        return self.client.query(f'SHOW CREATE TABLE `{self.database}`.`{table_name}`').result_rows[0][0]

    def get_system_setting(self, name):
        results = self.select('system.settings', f"name = '{name}'")
        if not results:
            return None
        return results[0].get('value', None)

    def get_max_record_version(self, table_name):
        """
        Query the maximum _version value for a given table directly from ClickHouse.
        
        Args:
            table_name: The name of the table to query
            
        Returns:
            The maximum _version value as an integer, or None if the table doesn't exist
            or has no records
        """
        try:
            if self.clickhouse_settings.cluster:
                table_name += self.DISTRIBUTED_TABLE_SUFFIX

            query = f"SELECT MAX(_version) FROM `{self.database}`.`{table_name}`"
            result = self.client.query(query)
            if not result.result_rows or result.result_rows[0][0] is None:
                logger.warning(f"No records with _version found in table {table_name}")
                return None
            return result.result_rows[0][0]
        except Exception as e:
            logger.error(f"Error querying max _version for table {table_name}: {e}")
            return None
