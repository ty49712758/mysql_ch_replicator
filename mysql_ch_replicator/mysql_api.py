import datetime
import time
import zoneinfo

import mysql.connector

from logging import getLogger

from .config import MysqlSettings
from .table_structure import TableStructure, TableField


logger = getLogger(__name__)


def format_utc_offset(offset: datetime.timedelta) -> str:
    """Format a UTC offset as a MySQL numeric time zone, e.g. '-05:30'."""
    total_seconds = int(offset.total_seconds())
    sign = '-' if total_seconds < 0 else '+'
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f'{sign}{hours:02d}:{minutes:02d}'


def set_session_time_zone(cursor, mysql_timezone: str):
    """
    Set the MySQL session time zone, so that MySQL returns TIMESTAMP values in
    mysql_timezone.

    Named time zones (e.g. 'Asia/Shanghai') are only known to the server when
    its time zone tables (mysql.time_zone*) have been populated. When they are
    not, MySQL rejects the statement with error 1298. Fall back to the
    equivalent numeric offset (e.g. '+08:00'), which the server always
    understands, so that replication does not fail on servers that simply have
    no time zone tables loaded.
    """
    try:
        cursor.execute(f"SET time_zone = '{mysql_timezone}'")
        return
    except mysql.connector.errors.DatabaseError as e:
        if e.errno != 1298:
            raise

    logger.warning(
        f'MySQL server does not recognize the time zone "{mysql_timezone}" '
        f'(error 1298 - its time zone tables are most likely not populated). '
        f'Falling back to a numeric UTC offset.',
    )

    offset = datetime.datetime.now(zoneinfo.ZoneInfo(mysql_timezone)).utcoffset()
    offset_str = format_utc_offset(offset)
    logger.warning(
        f'Using session time_zone = "{offset_str}" instead of "{mysql_timezone}". '
        f'The offset is fixed, so daylight saving transitions will not be applied. '
        f'Populate the MySQL time zone tables to use the named time zone.',
    )
    cursor.execute(f"SET time_zone = '{offset_str}'")


class MySQLApi:
    RECONNECT_INTERVAL = 3 * 60

    def __init__(self, database: str, mysql_settings: MysqlSettings, mysql_timezone: str = 'UTC'):
        self.database = database
        self.mysql_settings = mysql_settings
        self.mysql_timezone = mysql_timezone
        self.last_connect_time = 0
        self.reconnect_if_required()

    def close(self):
        self.db.close()
        self.last_connect_time = 0

    def reconnect_if_required(self, force=False):
        curr_time = time.time()
        if curr_time - self.last_connect_time < MySQLApi.RECONNECT_INTERVAL and not force:
            return
        conn_settings = dict(
            host=self.mysql_settings.host,
            port=self.mysql_settings.port,
            user=self.mysql_settings.user,
            passwd=self.mysql_settings.password,
        )
        # Use charset from config if available
        if hasattr(self.mysql_settings, 'charset'):
            conn_settings['charset'] = self.mysql_settings.charset
            # Set appropriate collation based on charset
            if self.mysql_settings.charset == 'utf8mb4':
                conn_settings['collation'] = 'utf8mb4_unicode_ci'
        try:
            self.db = mysql.connector.connect(**conn_settings)
        except mysql.connector.errors.DatabaseError as e:
            if 'Unknown collation' in str(e):
                conn_settings['charset'] = 'utf8mb4'
                conn_settings['collation'] = 'utf8mb4_general_ci'
                self.db = mysql.connector.connect(**conn_settings)
            else:
                raise
        self.cursor = self.db.cursor()

        if self.mysql_timezone and self.mysql_timezone != 'UTC':
            set_session_time_zone(self.cursor, self.mysql_timezone)

        if self.database is not None:
            self.cursor.execute(f'USE `{self.database}`')
        self.last_connect_time = curr_time

    def drop_database(self, db_name):
        self.cursor.execute(f'DROP DATABASE IF EXISTS `{db_name}`')

    def drop_table(self, table_name):
        self.cursor.execute(f'DROP TABLE IF EXISTS `{table_name}`')

    def create_database(self, db_name):
        self.cursor.execute(f'CREATE DATABASE `{db_name}`')

    def execute(self, command, commit=False, args=None):
        if args:
            self.cursor.execute(command, args)
        else:
            self.cursor.execute(command)
        if commit:
            self.db.commit()

    def set_database(self, database):
        self.reconnect_if_required()
        self.database = database
        self.cursor = self.db.cursor()
        self.cursor.execute(f'USE `{self.database}`')

    def get_databases(self):
        self.reconnect_if_required(True)  # New database appear only after new connection
        self.cursor.execute('SHOW DATABASES')
        res = self.cursor.fetchall()
        tables = [x[0] for x in res]
        return tables

    def get_tables(self):
        self.reconnect_if_required()
        self.cursor.execute('SHOW FULL TABLES')
        res = self.cursor.fetchall()
        tables = [x[0] for x in res if x[1] == 'BASE TABLE']
        return tables

    def get_binlog_files(self):
        self.reconnect_if_required()
        self.cursor.execute('SHOW BINARY LOGS')
        res = self.cursor.fetchall()
        tables = [x[0] for x in res]
        return tables

    def get_table_create_statement(self, table_name) -> str:
        self.reconnect_if_required()
        self.cursor.execute(f'SHOW CREATE TABLE `{table_name}`')
        res = self.cursor.fetchall()
        create_statement = res[0][1].strip()
        return create_statement

    def get_records(self, table_name, order_by, limit, start_value=None, worker_id=None, total_workers=None):
        self.reconnect_if_required()

        # Escape column names with backticks to avoid issues with reserved keywords like "key"
        order_by_escaped = [f'`{col}`' for col in order_by]
        order_by_str = ','.join(order_by_escaped)

        where = ''
        if start_value is not None:
            # Build the start_value condition for pagination
            start_value_str = ','.join(map(str, start_value))
            where = f'WHERE ({order_by_str}) > ({start_value_str}) '

        # Add partitioning filter for parallel processing (e.g., sharded crawling)
        if worker_id is not None and total_workers is not None and total_workers > 1:
            # Escape column names in COALESCE expressions
            coalesce_expressions = [f"COALESCE(`{key}`, '')" for key in order_by]
            concat_keys = f"CONCAT_WS('|', {', '.join(coalesce_expressions)})"
            hash_condition = f"CRC32({concat_keys}) % {total_workers} = {worker_id}"

            if where:
                where += f'AND {hash_condition} '
            else:
                where = f'WHERE {hash_condition} '

        # Construct final query
        query = f'SELECT * FROM `{table_name}` {where}ORDER BY {order_by_str} LIMIT {limit}'

#         print("Executing query:", query)

        # Execute the query
        self.cursor.execute(query)
        res = self.cursor.fetchall()
        
        if self.mysql_timezone and self.mysql_timezone != 'UTC':
            tz = zoneinfo.ZoneInfo(self.mysql_timezone)
            records = []
            for row in res:
                new_row = []
                for value in row:
                    if isinstance(value, datetime.datetime) and value.tzinfo is None:
                        value = value.replace(tzinfo=tz)
                    new_row.append(value)
                records.append(tuple(new_row))
            return records
        
        records = [x for x in res]
        return records
