import os
import json
import time
import sys
import threading
from uvicorn import Config, Server
from fastapi import APIRouter, FastAPI

from logging import getLogger

from .config import Settings
from .mysql_api import MySQLApi
from .monitoring import bnum
from .utils import ProcessRunner, GracefulKiller
from .binlog_replicator import DataReader

from . import db_replicator


logger = getLogger(__name__)



class BinlogReplicatorRunner(ProcessRunner):
    def __init__(self, config_file):
        super().__init__(f'{sys.argv[0]} --config {config_file} binlog_replicator')


class DbReplicatorRunner(ProcessRunner):
    def __init__(self, db_name, config_file, worker_id=None, total_workers=None, initial_only=False, skip_initial_replication=False):
        cmd = f'{sys.argv[0]} --config {config_file} --db {db_name} db_replicator'
        
        if worker_id is not None:
            cmd += f' --worker_id={worker_id}'
        
        if total_workers is not None:
            cmd += f' --total_workers={total_workers}'
        
        if initial_only:
            cmd += ' --initial_only=True'
        
        if skip_initial_replication:
            cmd += ' --skip_initial_replication'
            
        super().__init__(cmd)


class DbOptimizerRunner(ProcessRunner):
    def __init__(self, config_file):
        super().__init__(f'{sys.argv[0]} --config {config_file} db_optimizer')


class RunAllRunner(ProcessRunner):
    def __init__(self, db_name, config_file):
        super().__init__(f'{sys.argv[0]} --config {config_file} run_all --db {db_name}')


app = FastAPI()



class Runner:

    DB_REPLICATOR_RUN_DELAY = 5

    def __init__(self, config: Settings, wait_initial_replication: bool, databases: str):
        self.config = config
        self.databases = databases or config.databases
        self.wait_initial_replication = wait_initial_replication
        self.runners: dict[str, DbReplicatorRunner] = {}
        self.binlog_runner = None
        self.db_optimizer = None
        self.http_server = None
        self.router = None
        self.mysql_api = None

    def run_server(self):
        if not self.config.http_host or not self.config.http_port:
            logger.info('http server disabled')
            return
        logger.info('starting http server')

        config = Config(app=app, host=self.config.http_host, port=self.config.http_port)
        self.router = APIRouter()
        self.router.add_api_route("/health", self.health, methods=["GET"])
        app.include_router(self.router)

        self.http_server = Server(config)
        self.http_server.run()

    def health(self, binlog_replicator_lines: int = 3, db_optimizer_lines: int = 3, db_replicators_lines: int = 3):
        mysql_info = self._get_mysql_binlog_info()
        processes = self._get_processes_status(binlog_replicator_lines, db_optimizer_lines, db_replicators_lines)
        databases_status = self._get_databases_status(mysql_info)
        return {
            'status': self._get_overall_status(processes),
            'binlog': self._get_binlog_status(mysql_info, databases_status),
            'databases': databases_status,
            'processes': processes,
        }

    def _get_mysql_binlog_info(self):
        try:
            if self.mysql_api is None:
                return None
            files_sizes = self.mysql_api.get_binlog_files_with_sizes()
            master = self.mysql_api.get_master_status()
            latest_file = None
            if master:
                latest_file = master[0]
            elif files_sizes:
                latest_file = sorted(files_sizes, key=lambda item: bnum(item[0]))[-1][0]
            return {
                'master': master,
                'files_sizes': files_sizes,
                'latest_file': latest_file,
            }
        except Exception:
            logger.warning('failed to read mysql binlog info for health', exc_info=True)
            return None

    def _absolute_binlog_offset(self, file_name, pos, files_sizes):
        target_num = bnum(file_name)
        total = 0
        for name, size in files_sizes:
            if bnum(name) < target_num:
                total += size
        return total + pos

    def _compute_lag_bytes(self, transaction, latest_transaction, files_sizes):
        if transaction is None or latest_transaction is None or not files_sizes:
            return None
        try:
            latest_offset = self._absolute_binlog_offset(latest_transaction[0], latest_transaction[1], files_sizes)
            tx_offset = self._absolute_binlog_offset(transaction[0], transaction[1], files_sizes)
            return latest_offset - tx_offset
        except Exception:
            logger.warning('failed to compute byte-level lag for health', exc_info=True)
            return None

    def _get_binlog_state(self):
        file_path = os.path.join(self.config.binlog_replicator.data_dir, 'state.json')
        if not os.path.exists(file_path):
            return None
        with open(file_path, 'rt') as f:
            return json.load(f)

    def _get_binlog_status(self, mysql_info, databases_status):
        last_seen_transaction = None
        try:
            state = self._get_binlog_state()
            if state and state.get('last_seen_transaction') is not None:
                last_seen_transaction = state['last_seen_transaction']
        except Exception:
            logger.warning('failed to read binlog state for health', exc_info=True)

        lag_values = [
            status.get('lag_bytes')
            for status in databases_status.values()
            if status.get('lag_bytes') is not None
        ]
        lag_bytes = max(lag_values) if lag_values else None

        return {
            'mysql_latest': mysql_info.get('latest_file') if mysql_info else None,
            'replicator_position': list(last_seen_transaction) if last_seen_transaction else None,
            'lag_bytes': lag_bytes,
        }

    def _get_databases_status(self, mysql_info):
        return {
            db_name: self._get_database_status(db_name, mysql_info)
            for db_name in list(self.runners.keys())
        }

    def _get_database_status(self, db_name, mysql_info):
        state_path = os.path.join(
            self.config.binlog_replicator.data_dir,
            db_name,
            'state.pckl',
        )
        try:
            state = db_replicator.State(state_path)
        except Exception:
            logger.warning(f'failed to read state for {db_name} for health', exc_info=True)
            return {
                'status': None,
                'status_code': None,
                'last_processed_transaction': None,
                'lag_bytes': None,
            }

        last_processed = state.last_processed_transaction
        last_available = self._get_last_available_transaction(db_name)
        files_sizes = mysql_info.get('files_sizes') if mysql_info else None

        return {
            'status': state.status.name if state.status else None,
            'status_code': state.status.value if state.status else None,
            'last_processed_transaction': list(last_processed) if last_processed else None,
            'lag_bytes': self._compute_lag_bytes(last_processed, last_available, files_sizes),
        }

    def _get_last_available_transaction(self, db_name):
        try:
            reader = DataReader(self.config.binlog_replicator, db_name)
            return reader.get_last_transaction_id()
        except Exception:
            logger.warning(f'failed to read last available transaction for {db_name} for health', exc_info=True)
            return None

    def _get_processes_status(self, binlog_replicator_lines, db_optimizer_lines, db_replicators_lines):
        data_dir = self.config.binlog_replicator.data_dir
        db_processes = {
            db_name: self._get_process_status(
                runner,
                os.path.join(data_dir, db_name, 'db_replicator.log'),
                db_replicators_lines,
            )
            for db_name, runner in list(self.runners.items())
        }
        return {
            'binlog_replicator': self._get_process_status(
                self.binlog_runner,
                os.path.join(data_dir, 'binlog_replicator.log'),
                binlog_replicator_lines,
            ),
            'db_optimizer': self._get_process_status(
                self.db_optimizer,
                os.path.join(data_dir, 'db_optimizer.log'),
                db_optimizer_lines,
            ),
            'db_replicators': db_processes,
        }

    def _get_process_status(self, runner, log_file, lines):
        if runner is None or runner.process is None:
            status = {'state': 'not_started', 'alive': False, 'last_exit_code': None}
        elif runner.poll() is None:
            status = {'state': 'running', 'alive': True, 'last_exit_code': runner.last_exit_code}
        else:
            status = {'state': 'exited', 'alive': False, 'last_exit_code': runner.last_exit_code}
        status['logs'] = self._read_last_lines(log_file, lines)
        return status

    def _read_last_lines(self, file_path, n):
        n = max(0, min(n, 100))
        if n == 0:
            return []
        try:
            with open(file_path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = 64 * 1024
                data = b''
                pos = size
                while pos > 0 and data.count(b'\n') <= n:
                    read_size = min(chunk, pos)
                    pos -= read_size
                    f.seek(pos)
                    data = f.read(read_size) + data
                lines = data.decode('utf-8', errors='replace').splitlines()
                return lines[-n:]
        except FileNotFoundError:
            return []
        except Exception:
            logger.warning(f'failed to read log tail for {file_path}', exc_info=True)
            return []

    def _get_overall_status(self, processes):
        states = [
            processes['binlog_replicator']['state'],
            processes['db_optimizer']['state'],
        ]
        states.extend(p['state'] for p in processes['db_replicators'].values())
        if 'exited' in states:
            return 'degraded'
        if 'not_started' in states:
            return 'starting'
        return 'ok'

    def is_initial_replication_finished(self, db_name):
        state_path = os.path.join(
            self.config.binlog_replicator.data_dir,
            db_name,
            'state.pckl',
        )
        state = db_replicator.State(state_path)
        return state.status == db_replicator.Status.RUNNING_REALTIME_REPLICATION

    def restart_dead_processes(self):
        for runner in self.runners.values():
            runner.restart_dead_process_if_required()
        if self.binlog_runner is not None:
            self.binlog_runner.restart_dead_process_if_required()
        if self.db_optimizer is not None:
            self.db_optimizer.restart_dead_process_if_required()

    def check_databases_updated(self, mysql_api: MySQLApi):
        logger.debug('check if databases were created / removed in mysql')
        databases = mysql_api.get_databases()
        logger.info(f'mysql databases: {databases}')
        databases = [db for db in databases if self.config.is_database_matches(db)]
        logger.info(f'mysql databases filtered: {databases}')
        for db in databases:
            if db in self.runners:
                continue
            logger.info(f'running replication for {db} (database created in mysql)')
            runner = self.runners[db] = DbReplicatorRunner(db_name=db, config_file=self.config.settings_file)
            runner.run()

        for db in self.runners.keys():
            if db in databases:
                continue
            logger.info(f'stop replication for {db} (database removed from mysql)')
            self.runners[db].stop()
            self.runners.pop(db)

    def run(self):
        self.mysql_api = MySQLApi(
            database=None,
            mysql_settings=self.config.mysql,
            mysql_timezone=self.config.mysql_timezone,
        )
        databases = self.mysql_api.get_databases()
        databases = [db for db in databases if self.config.is_database_matches(db)]

        killer = GracefulKiller()

        self.binlog_runner = BinlogReplicatorRunner(self.config.settings_file)
        self.binlog_runner.run()

        self.db_optimizer = DbOptimizerRunner(self.config.settings_file)
        self.db_optimizer.run()

        server_thread = threading.Thread(target=self.run_server, daemon=True)
        server_thread.start()

        t1 = time.time()
        while time.time() - t1 < self.DB_REPLICATOR_RUN_DELAY and not killer.kill_now:
            time.sleep(0.3)

        # First - continue replication for DBs that already finished initial replication
        for db in databases:
            if killer.kill_now:
                break
            if not self.is_initial_replication_finished(db_name=db):
                continue
            logger.info(f'running replication for {db} (initial replication finished)')
            runner = self.runners[db] = DbReplicatorRunner(db_name=db, config_file=self.config.settings_file)
            runner.run()

        # Second - run replication for other DBs one by one and wait until initial replication finished
        for db in databases:
            if db in self.runners:
                continue
            if killer.kill_now:
                break

            logger.info(f'running replication for {db} (initial replication not finished - waiting)')
            runner = self.runners[db] = DbReplicatorRunner(db_name=db, config_file=self.config.settings_file)
            runner.run()
            if not self.wait_initial_replication:
                continue

            while not self.is_initial_replication_finished(db_name=db) and not killer.kill_now:
                time.sleep(1)
                self.restart_dead_processes()

        logger.info('all replicators launched')

        last_check_db_updated = time.time()
        while not killer.kill_now:
            time.sleep(1)
            self.restart_dead_processes()
            if time.time() - last_check_db_updated > self.config.check_db_updated_interval:
                self.check_databases_updated(mysql_api=self.mysql_api)
                last_check_db_updated = time.time()

        logger.info('stopping runner')

        if self.binlog_runner is not None:
            logger.info('stopping binlog replication')
            self.binlog_runner.stop()

        if self.db_optimizer is not None:
            logger.info('stopping db_optimizer')
            self.db_optimizer.stop()

        for db_name, db_replication_runner in self.runners.items():
            logger.info(f'stopping replication for {db_name}')
            db_replication_runner.stop()

        if self.http_server:
            self.http_server.should_exit = True

        server_thread.join()

        logger.info('stopped')