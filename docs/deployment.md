# 部署指南(生产服务器)

本文档说明如何把 `mysql_ch_replicator` 打包并部署到 Linux 生产服务器,以 systemd 常驻方式运行。

- [1. 部署形态与硬性约束](#1-部署形态与硬性约束)
- [2. 打包](#2-打包)
- [3. 服务器安装](#3-服务器安装)
- [4. 配置文件](#4-配置文件)
- [5. systemd 常驻](#5-systemd-常驻)
- [6. 服务器前置条件](#6-服务器前置条件)
- [7. Docker 部署](#7-docker-部署)
- [8. 升级流程](#8-升级流程)
- [9. 故障排查](#9-故障排查)

---

## 1. 部署形态与硬性约束

`run_all` 是一个**父进程 + 子进程**的监管进程(`mysql_ch_replicator/runner.py`)。父进程会在需要时把自己**再次拉起**,执行的命令由下面这行拼接而成:

```python
f'{sys.argv[0]} --config {config_file} db_replicator'
```

并且它是用 `subprocess.Popen(cmd.split())` 执行的 —— **不经过 shell,也不做 PATH 之外的路径解析**。

由此得出两条必须遵守的规则:

1. **必须用安装出来的 `mysql_ch_replicator` 命令启动,不要用 `python main.py`。**
   `python main.py` 的 `sys.argv[0]` 是 `main.py`,`Popen(['main.py', ...])` 既不是绝对路径、也不在 PATH 上,子进程无法启动,表现为父进程活着但没有任何数据库被复制。

2. **`ExecStart` 用绝对路径**(如 `/opt/mysql-ch-replicator/.venv/bin/mysql_ch_replicator`),避免依赖 `WorkingDirectory`。

其它需要知道的:

- 程序自带重启逻辑:子进程挂掉会自动重启;父进程还会按 `auto_restart_interval`(默认 3600 秒)定期重启整体以降内存。因此外层用 `Restart=always` 只是兜底。
- 程序内部有 `GracefulKiller` 处理 `SIGTERM`,systemd 默认的 `KillMode=control-group` 会把信号发给整个 cgroup,能干净退出。

---

## 2. 打包

在开发机上执行。项目使用 `poetry-core` 作为构建后端:

```bash
cd /path/to/mysql_ch_replicator

# 建议用虚拟环境
python3 -m venv .venv
source .venv/bin/activate

pip install -U pip build
python -m build          # 产出 dist/mysql_ch_replicator-<version>-py3-none-any.whl
```

等价地也可以用 Poetry(上游 CI 用的就是它):

```bash
poetry build
```

### 关于 C++ 扩展

`binlog_json_parser/` 是 C++ 扩展(用于加速 MySQL JSON 解析)。**仓库中已提交预编译产物**,会被自动打进 wheel:

| 文件 | 平台 |
| --- | --- |
| `libmysqljsonparse_x86_64.so` | Linux x86-64 |
| `libmysqljsonparse.so` | Linux ARM64 |
| `libmysqljsonparse.dylib` | macOS |

运行时由 `mysql_ch_replicator/pymysqlreplication/cpp_accelerated.py` 按 `platform.system()` / `platform.machine()` 自动选择。因此:

- **目标服务器上不需要安装编译工具链**,也不需要执行 `binlog_json_parser/build_static.sh`。
- 该脚本仅在预编译库与目标平台不兼容(例如 glibc 版本过旧导致 `GLIBC_... not found`)时才需要,且它依赖 `docker buildx`。

> ⚠️ **wheel 的标签是 `py3-none-any`(纯 Python 标签),但内容包含平台相关的 `.so`。**
> 在 Linux 上安装运行正常;**在 Windows 上 `pip install` 能装上,但一运行就会在 `cpp_accelerated.py` 报 `TypeError: join() argument must be str, bytes, or os.PathLike object, not 'NoneType'`** —— 因为该文件没有 Windows 分支,也没有 `.dll`。所以只把这个 wheel 装到 Linux 服务器。

### 版本管理

给 fork 打 tag 固定版本,保证部署可复现:

```bash
git tag -a v0.1.18-fix -m "timezone + partition nullable fixes"
git push origin v0.1.18-fix
```

---

## 3. 服务器安装

以下命令在目标 Linux 服务器上执行(示例目录,可按需调整)。

### 3.1 创建专用用户与目录

```bash
sudo useradd -r -m -d /opt/mysql-ch-replicator replicator
sudo mkdir -p /opt/mysql-ch-replicator /etc/mysql-ch-replicator
sudo chown replicator:replicator /opt/mysql-ch-replicator
```

### 3.2 建立虚拟环境并安装 wheel

先把 `dist/*.whl` 传到服务器(如 `/tmp/`),然后:

```bash
sudo -u replicator bash -lc '
  cd /opt/mysql-ch-replicator
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install /tmp/mysql_ch_replicator-0.1.18-py3-none-any.whl
'
```

为什么不装到系统 Python:符合 PEP 668(Ubuntu 23.04+ / Debian 12+ 禁止全局 `pip install`),同时隔离依赖。

也可以用 `pipx` 简化:

```bash
pipx install /tmp/mysql_ch_replicator-0.1.18-py3-none-any.whl
# 命令会出现在 ~/.local/bin/mysql_ch_replicator
```

当前环境要求 **Python 3.9+**(`pyproject.toml` 声明 `^3.9`;README 推荐 3.10 及以上)。

### 3.3 验证安装

```bash
# 命令可用
/opt/mysql-ch-replicator/.venv/bin/mysql_ch_replicator --help

# C++ 扩展可加载(关键:确认 .so 与目标平台兼容)
/opt/mysql-ch-replicator/.venv/bin/python -c \
  "from mysql_ch_replicator.pymysqlreplication import cpp_accelerated as c; c.test_func(); print('so OK')"
```

第二条应输出 `so OK`。

---

## 4. 配置文件

**配置文件不要放进软件包目录**,单独放在 `/etc` 下,便于升级时不被覆盖,也避免密码随包分发。

```bash
sudo cp example_config.yaml /etc/mysql-ch-replicator/config.yaml
sudo chmod 600 /etc/mysql-ch-replicator/config.yaml      # 内含数据库密码
sudo chown replicator:replicator /etc/mysql-ch-replicator/config.yaml
```

### 4.1 数据目录

`binlog_replicator.data_dir` 必须指向**本机磁盘**上的持久目录 —— 程序用它保存复制状态和 binlog 缓存文件。**不要用 NFS 等网络文件系统**(IO 慢、文件锁行为不一致)。

```yaml
binlog_replicator:
  data_dir: '/var/lib/mysql-ch-replicator/data'   # 必须是空目录
  records_per_file: 100000
  binlog_retention_period: 43200                  # 本地 binlog 缓存保留时长(秒),默认 12h
```

```bash
sudo mkdir -p /var/lib/mysql-ch-replicator/data
sudo chown -R replicator:replicator /var/lib/mysql-ch-replicator
```

> 磁盘规划:该目录会缓存 binlog 事件,按写入量留足空间;超过 `binlog_retention_period` 的缓存会被自动清理。

### 4.2 时区

强烈建议显式设置 `mysql_timezone`,并与 MySQL 服务端、ClickHouse 服务端时区保持一致:

```yaml
mysql_timezone: 'Asia/Shanghai'
```

关于时区的两点说明:

1. **数据正确性以 `mysql_timezone` 为准。** 本 fork 已修复实时(binlog)复制路径忽略该配置、导致时间值随进程所在时区偏移的问题。修复后,写入 ClickHouse 的时刻只由 `mysql_timezone` 决定,不再依赖进程时区。
2. 仍然建议给进程设置 `TZ`(见 systemd 一节)。这不是为了数据正确性,而是让**日志时间戳**显示为本地时间,便于排障。**注意环境变量优先级高于系统时区**:即使系统时区已是 `Asia/Shanghai`,若启动环境里存在 `TZ=UTC`,日志就会按 UTC 记录,容易误判。

### 4.3 其它生产建议

按需配置(详见 `README.md` 的配置章节):

```yaml
log_level: 'info'
initial_replication_threads: 8      # 初次全量复制加速,建议设为 CPU 核数
optimize_interval: 86400            # 定期 OPTIMIZE,避免存储膨胀
auto_restart_interval: 3600         # 定期重启降内存
```

---

## 5. systemd 常驻

创建 `/etc/systemd/system/mysql-ch-replicator.service`:

```ini
[Unit]
Description=mysql_ch_replicator (MySQL -> ClickHouse)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=replicator
Group=replicator
WorkingDirectory=/opt/mysql-ch-replicator
Environment=TZ=Asia/Shanghai
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/mysql-ch-replicator/.venv/bin/mysql_ch_replicator --config /etc/mysql-ch-replicator/config.yaml run_all
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

要点:

- `ExecStart` **必须使用绝对路径**(见[第 1 节](#1-部署形态与硬性约束))。
- `Restart=always` 为兜底;程序自身已有子进程重启与定期重启机制。
- 不要设置 `KillMode`(保持默认的 `control-group`),使 SIGTERM 能传给父进程和所有子进程,配合内部的 `GracefulKiller` 实现优雅退出。
- `Environment=TZ=...` 仅影响日志时间戳。

启用并启动:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mysql-ch-replicator
sudo systemctl status mysql-ch-replicator

# 查看实时日志
sudo journalctl -u mysql-ch-replicator -f
```

### 内置 HTTP 控制接口(可选)

配置 `http_host` / `http_port` 后可远程触发操作,例如重启复制:

```yaml
http_host: '127.0.0.1'
http_port: 9128
```

```bash
curl http://127.0.0.1:9128/restart_replication
```

接口文档在 `/docs`。生产环境建议绑定 `127.0.0.1` 或置于防火墙/反向代理之后。

---

## 6. 服务器前置条件

上线前逐条确认(摘自 `README.md`,漏掉任何一条都可能导致复制异常)。

### MySQL

服务端配置需包含:

```ini
[mysqld]
binlog_format = ROW
gtid_mode = on
enforce_gtid_consistency = 1
binlog_expire_logs_seconds = 864000
max_binlog_size = 500M
```

账号授权:

```sql
GRANT SELECT, SUPER, REPLICATION SLAVE ON *.* TO 'user'@'%' IDENTIFIED BY 'password';
```

> 若使用 AWS RDS,在 `Parameter groups` 中设置 `binlog_format=ROW`。

### ClickHouse

必须开启 `final` 设置,否则更新会产生重复行。在 `override.xml`(或 `users.xml`)中:

```xml
<clickhouse>
    <profiles>
        <default>
            <final>1</final>
            <max_query_size>300000000</max_query_size>
            <max_ast_elements>1000000</max_ast_elements>
            <max_expanded_ast_elements>1000000</max_expanded_ast_elements>
        </default>
    </profiles>
</clickhouse>
```

确认生效:

```sql
SELECT name, value, changed FROM system.settings WHERE name = 'final';
-- value 应为 1
```

### 其它

- **时区**:MySQL、ClickHouse、`mysql_timezone` 三者保持一致。
- **网络**:服务器到 MySQL 与 ClickHouse 的端口需连通。
- **鉴权**:MySQL 需允许密码认证(`mysql_native_password` 或对应插件)。

---

## 7. Docker 部署

如果更习惯容器化,使用仓库根目录的 `Dockerfile` **自行构建**:

> ⚠️ **不要直接使用上游镜像 `fippo/mysql-ch-replicator:latest`。** 它是上游版本,不包含本 fork 的修复。

```bash
docker build -t mysql-ch-replicator:0.1.18-fix .

docker run -d --restart=always --name mysql-ch-replicator \
  -e TZ=Asia/Shanghai \
  -v /etc/mysql-ch-replicator/config.yaml:/app/config.yaml:ro \
  -v /var/lib/mysql-ch-replicator/data:/app/data \
  mysql-ch-replicator:0.1.18-fix \
  --config /app/config.yaml run_all
```

也可用 compose:

```yaml
services:
  mysql-ch-replicator:
    image: mysql-ch-replicator:0.1.18-fix
    restart: always
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - /etc/mysql-ch-replicator/config.yaml:/app/config.yaml:ro
      - /var/lib/mysql-ch-replicator/data:/app/data
    command: --config /app/config.yaml run_all
```

注意:

- 容器的 `TZ` **默认是 UTC**,务必显式设置(见 [4.2](#42-时区))。
- `data_dir` 在容器内是 `/app/data`,必须挂载到宿主机持久卷。
- 凭据也可用环境变量覆盖:`MYSQL_HOST`/`MYSQL_PORT`/`MYSQL_USER`/`MYSQL_PASSWORD`/`MYSQL_CHARSET`,`CLICKHOUSE_HOST`/`CLICKHOUSE_PORT`/`CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD`。

Docker 的优点是把 Python 环境一并固化;缺点是排查问题时需进入容器。两种方式择一即可。

---

## 8. 升级流程

```bash
# ── 开发机 ──────────────────────────────
git tag v0.1.19 && git push --tags
python -m build
scp dist/mysql_ch_replicator-0.1.19-py3-none-any.whl server:/tmp/

# ── 服务器 ──────────────────────────────
sudo -u replicator /opt/mysql-ch-replicator/.venv/bin/pip install \
    --force-reinstall /tmp/mysql_ch_replicator-0.1.19-py3-none-any.whl
sudo systemctl restart mysql-ch-replicator
sudo systemctl status mysql-ch-replicator
```

`data_dir` 中的状态文件无需改动,重启后会从上次记录的 binlog 位点继续复制。

> **注意**:升级只更新代码,不会重写已经写入 ClickHouse 的历史数据。如果某个 bug 曾产生错误数据(例如时区偏移),需要按 [9.2](#92-历史数据时区错误) 单独修复存量数据。

---

## 9. 故障排查

### 9.1 父进程存活但没有复制任何数据

几乎总是因为启动方式不对 —— `sys.argv[0]` 无法被子进程复用。检查:

```bash
systemctl show -p ExecStart mysql-ch-replicator
```

`ExecStart` 必须是**绝对路径**的 `mysql_ch_replicator` 命令,而不是 `python main.py`。

### 9.2 历史数据时区错误

若在修复前已经写入了偏移数据,重启不会自动纠正。按 README 的"从头再来"方式重做该库:

```bash
sudo systemctl stop mysql-ch-replicator

# 清空该库在 ClickHouse 中的目标表(按需保留备份)
# 删除复制状态,使其重新执行初始全量复制
sudo rm -rf /var/lib/mysql-ch-replicator/data

sudo systemctl start mysql-ch-replicator
```

初始复制会从 MySQL 重新拉取全量数据。

### 9.3 启动时报 `Unknown or incorrect time zone: 'Asia/Shanghai'`

MySQL 服务端的时区表(`mysql.time_zone*`)未导入。本 fork 已做兼容:会自动退化为等价的数字偏移(如 `+08:00`)并在日志中告警,不会中断复制。

如需使用命名时区,在 MySQL 上导入时区表:

```bash
mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root -p mysql
```

### 9.4 `GLIBC_... not found` 或 `.so` 加载失败

预编译的 C++ 库与目标系统 glibc 不兼容。需在目标环境重新编译:

```bash
cd binlog_json_parser
./build_static.sh          # 依赖 docker buildx
```

### 9.5 在 Windows 上运行报 `join() argument must be ... not 'NoneType'`

本工具**不支持 Windows 原生运行**(`cpp_accelerated.py` 无 Windows 分支,且未提供 `.dll`)。

- 请改用 WSL2、Linux 服务器或 Docker。
- 详见 [第 2 节](#关于-c-扩展)的说明。

### 9.6 日志时间戳与实际时间相差整数小时

进程环境的 `TZ` 与系统时区不一致(环境变量优先于系统时区)。确认并统一:

```bash
date
python3 -c "import datetime; print(datetime.datetime.now().astimezone())"
systemctl show -p Environment mysql-ch-replicator
```

> 这只影响日志显示。若**数据本身**有偏移,请检查 `mysql_timezone` 是否与 MySQL/ClickHouse 服务端时区一致。
