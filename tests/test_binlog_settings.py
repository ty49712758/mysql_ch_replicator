import pytest

from mysql_ch_replicator.binlog_replicator import BinlogReplicator
from mysql_ch_replicator.config import BinlogReplicatorSettings, Settings
from mysql_ch_replicator.pymysqlreplication.binlogstream import ReportSlave


def test_connection_tuning_defaults_to_unset():
    """Without configuration the pymysql defaults are kept."""
    settings = BinlogReplicatorSettings()

    assert settings.connect_timeout is None
    assert settings.heartbeat is None


@pytest.mark.parametrize('field,value', [
    ('connect_timeout', 0),
    ('connect_timeout', -1),
    ('connect_timeout', '30'),
    ('heartbeat', 0),
    ('heartbeat', -1),
    ('heartbeat', '30'),
])
def test_invalid_values_are_rejected(field, value):
    settings = BinlogReplicatorSettings(**{field: value})

    with pytest.raises(ValueError):
        settings.validate()


@pytest.mark.parametrize('field,value', [
    ('connect_timeout', 30),
    ('heartbeat', 15),
])
def test_valid_values_pass_validation(field, value):
    settings = BinlogReplicatorSettings(**{field: value})

    settings.validate()

    assert getattr(settings, field) == value


def write_config(tmp_path, binlog_replicator):
    config = tmp_path / 'config.yaml'
    lines = [
        'mysql:',
        "  host: 'localhost'",
        'clickhouse:',
        "  host: 'localhost'",
        "databases: 'test'",
        'binlog_replicator:',
        f"  data_dir: '{tmp_path / 'binlog'}'",
    ]
    lines += [f'  {key}: {value}' for key, value in binlog_replicator.items()]
    config.write_text('\n'.join(lines) + '\n')
    return str(config)


def test_values_are_read_from_the_config_file(tmp_path):
    settings = Settings()
    settings.load(write_config(tmp_path, {'connect_timeout': 30, 'heartbeat': 20}))

    assert settings.binlog_replicator.connect_timeout == 30
    assert settings.binlog_replicator.heartbeat == 20


def make_replicator(tmp_path, **binlog_replicator):
    settings = Settings()
    settings.load(write_config(tmp_path, binlog_replicator))
    settings.binlog_replicator.validate()
    return BinlogReplicator(settings)


def connection_settings_of(replicator):
    # Name-mangled attribute of BinLogStreamReader; there is no accessor.
    return replicator.stream._BinLogStreamReader__connection_settings


def test_unset_values_do_not_touch_the_stream(tmp_path):
    replicator = make_replicator(tmp_path)

    assert 'connect_timeout' not in connection_settings_of(replicator)
    assert replicator.stream.slave_heartbeat is None


def test_configured_values_are_forwarded_to_the_stream(tmp_path):
    replicator = make_replicator(tmp_path, connect_timeout=30, heartbeat=20)

    assert connection_settings_of(replicator)['connect_timeout'] == 30
    assert replicator.stream.slave_heartbeat == 20


def test_password_is_not_written_to_the_log(tmp_path, caplog):
    """BinLogStreamReader logs its parameters on startup - never the password."""
    settings = Settings()
    settings.load(write_config(tmp_path, {}))
    settings.binlog_replicator.validate()
    settings.mysql.password = 'sup3r-s3cret'

    with caplog.at_level('INFO'):
        BinlogReplicator(settings)

    assert 'sup3r-s3cret' not in caplog.text
    # the parameters are still logged, with the secret masked out
    assert "'passwd': '***'" in caplog.text


def test_report_slave_repr_hides_the_password():
    slave = ReportSlave(('host', 'user', 's3cret', 3306))

    assert 's3cret' not in repr(slave)
