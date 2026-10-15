"""
Local pytest runner for machines without the prebuilt native library
(libmysqljsonparse) - e.g. Windows.

`import mysql_ch_replicator` loads that library via ctypes at import time and
fails when it is missing, which blocks even pure-logic tests. This script
stubs the native module before the package is imported, then hands over to
pytest, so all the usual options keep working:

    python run_tests_local.py tests/test_mysql_timezone.py -v
    python run_tests_local.py tests/test_mysql_timezone.py -k fallback

Note: the stub only replaces the C++ binlog JSON parser. Tests that actually
exercise binlog row parsing, or that talk to MySQL/ClickHouse, still have to
run on Linux via docker (see README "Running Tests").
"""
import sys
import types


NATIVE_MODULE = 'mysql_ch_replicator.pymysqlreplication.cpp_accelerated'


def stub_native_library():
    if NATIVE_MODULE in sys.modules:
        return
    stub = types.ModuleType(NATIVE_MODULE)
    stub.cpp_mysql_to_json = lambda data: data
    sys.modules[NATIVE_MODULE] = stub


if __name__ == '__main__':
    stub_native_library()
    import pytest
    sys.exit(pytest.main(sys.argv[1:]))
