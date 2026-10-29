# T2 · cluster 模块任务单

| 项 | 内容 |
|---|---|
| 任务编号 | T2 |
| 模块 | M2 集群管理 |
| 依赖 | T1 契约层（`raftlab/schemas/*`） |
| 预估 | 2 天（P0 约 1.5 天，P1 +0.5 天） |
| 交付形态 | `raftlab/cluster.py` + `raftlab` CLI 子命令 + `cluster.json` |
| 评审人 | 人工（联调与验收） |

---

## 1. 目标

用一条命令拉起 / 查询 / 停止一套 Raft KV 集群，并产出符合 `cluster.schema.json` 的 `cluster.json`。**只做进程与端口生命周期，不做压测、不做故障注入。**

## 2. 你无法假设、必须适配的现状

阅读 `example/raftCoreExample/raftKvDB.cpp` 与 `src/raftCore/kvServer.cpp` 后会确认：

| 事实 | 影响 |
|---|---|
| `raftCoreRun -n N -f test.conf` 会 **fork N 个子进程**，父进程 `pause()` | 停集群必须杀进程组，不能只杀父进程 |
| 每个子进程 `KvServer` 构造时先起 RPC server，再 `sleep(6)`，读配置，再 `sleep(N-i)`，最后 `raft.init()` | 端口监听远早于“集群可用”，就绪判定不能只看 TCP |
| 每个节点把自己的 `node{i}ip=...` / `node{i}port=...` **追加**写入 **当前工作目录** 的 `test.conf` | 必须 `cwd=workdir`；配置文件是发现端口的唯一来源 |
| 端口随机（10000~29999），由 `main` 里的 `startPort` 决定 | 必须解析配置文件，不能自己算 |
| `Persister` 把 `raftstatePersist{i}.txt`、`snapshotPersist{i}.txt` 写在 CWD | 每个 run 独立 workdir，避免互相污染 |
| `main` 启动时会把 `test.conf` 截断 | 同一 workdir 重复 `up` 会覆盖旧配置 |

## 3. 交付物

```
raftlab/
├── cluster.py          # 新增：Cluster / NodeInfo / ClusterInfo / ClusterError
└── cli.py              # 修改：新增 cluster 子命令（up/status/down/restart）
```

不得修改 `raftlab/schema.py`、`raftlab/schemas/*`、`scenarios/*`、`tests/fixtures/*`。

## 4. 接口

### 4.1 Python API

```python
class ClusterError(RuntimeError):
    """运行期失败（起不来、超时、进程提前退出）。"""

@dataclass
class NodeInfo:
    index: int
    pid: int
    ip: str
    port: int
    log_file: str        # 相对 workdir

@dataclass
class ClusterInfo:
    config_file: str
    started_at: str      # UTC ISO-8601 毫秒
    ready_at: str | None
    nodes: list[NodeInfo]

class Cluster:
    def __init__(
        self,
        nodes: int,
        workdir: Path,
        binary: Path,                  # 默认 <repo>/bin/raftCoreRun
        max_raft_state: int = 500,     # 预留：当前二进制写死 500，见 §8
        startup_timeout: float = 30.0,
        startup_grace: float | None = None,   # 默认 8 + nodes 秒
    ) -> None: ...

    def start(self) -> ClusterInfo: ...
    def wait_ready(self, timeout: float | None = None) -> None: ...
    def status(self) -> ClusterInfo: ...
    def stop(self, timeout: float = 5.0) -> None: ...
    def restart(self, index: int | None = None) -> None: ...   # P1
```

### 4.2 CLI

```bash
python3 -m raftlab cluster up     --nodes 3 --workdir runs/t2 [--timeout 30] [--json]
python3 -m raftlab cluster status --workdir runs/t2 [--json]
python3 -m raftlab cluster down   --workdir runs/t2 [--timeout 5]
python3 -m raftlab cluster restart --workdir runs/t2 [--node 1]      # P1
```

退出码沿用全局约定：`0` 成功，`1` 运行期失败，`2` 用法/环境错误（二进制不存在、workdir 不可写、已在运行）。

`--json` 输出 `ClusterInfo` 的字典形式，字段与 `cluster.json` 一致。

## 5. 行为规范

### 5.1 `start()`

1. 校验 `binary` 存在且可执行，否则 `exit 2`。
2. `workdir.mkdir(parents=True, exist_ok=True)`。
3. 如果 `workdir/cluster.json` 存在且其中进程仍存活 → 报“already running”，`exit 2`；若进程已死则允许覆盖。
4. 清理 `workdir/test.conf`、`raftstatePersist*.txt`、`snapshotPersist*.txt`？**不清理持久化文件**（restart 场景需要），只删除旧的 `test.conf`。
5. 启动：
   ```python
   subprocess.Popen(
       [str(binary), "-n", str(nodes), "-f", "test.conf"],
       cwd=workdir,
       stdout=log_fh, stderr=subprocess.STDOUT,
       start_new_session=True,          # 关键：独立进程组
       text=True,
   )
   ```
   `log_fh` 指向 `workdir/cluster.log`。
6. 轮询 `test.conf`，直到出现全部 `node{i}ip` / `node{i}port`（超时 `startup_timeout`，每 100ms 一次）。期间若父进程已退出 → 附带 `cluster.log` 末尾 20 行报错。
7. 解析端口，通过 `pgrep -P <parent_pid>` 或 `ss -ltnp` 把 `(index, port)` 映射到子进程 pid。
8. 写 `cluster.json`（`ready_at` 暂为 `null`）。
9. 调用 `wait_ready()`。

### 5.2 `wait_ready()`

**P0 判定**（没有状态接口时的现实选择）：

- 所有节点端口 `TCP connect` 成功；
- 且自启动起已过 `startup_grace` 秒（默认 `8 + nodes`，对应 `sleep(6)` + `sleep(N-i)` + 选举）；
- 期间进程不得退出（轮询 `pid` 存活）。

成功后补写 `ready_at` 并覆盖 `cluster.json`。

> 真正的 leader 判定依赖 `Status` RPC（T5）。本任务不实现，`wait_ready` 不得假装知道 leader。

### 5.3 `status()`

- 读 `cluster.json`；
- 对每个 pid 检查存活（`os.kill(pid, 0)`）；
- 返回 `ClusterInfo`，额外打印 `alive/total`；
- 有节点死亡时退出码为 `1`。

### 5.4 `stop()`

1. 读 `cluster.json` 取父 pid（若文件不存在，按 `pgrep -f "bin/raftCoreRun"` 兜底并提示）。
2. `os.killpg(os.getpgid(pid), SIGTERM)`，等待 `timeout`；
3. 未退出则 `SIGKILL`；
4. 等待子进程被回收，确认无残留（`pgrep -P` 为空）；
5. **保留** `cluster.json`（记录现场），不删除 workdir。

### 5.5 异常与幂等

| 场景 | 行为 |
|---|---|
| 二进制缺失 | `exit 2`，提示编译命令 |
| workdir 不可写 | `exit 2` |
| 端口全部未出现（超时） | `exit 1`，打印 `cluster.log` 尾部 |
| 父进程提前退出 | `exit 1`，打印退出码与日志尾部 |
| 重复 `up`（进程存活） | `exit 2`，“already running” |
| `down` 时进程已死 | `exit 0`，幂等 |
| `status` 时无 `cluster.json` | `exit 2` |

## 6. `cluster.json`

必须通过 `python3 -m raftlab schema --check <workdir>/cluster.json`。示例：

```json
{
  "schemaVersion": "1.0",
  "configFile": "runs/t2/test.conf",
  "startedAt": "2026-10-27T01:10:01.000Z",
  "readyAt": "2026-10-27T01:10:12.400Z",
  "nodes": [
    { "index": 0, "pid": 4101, "ip": "127.0.1.1", "port": 29016, "logFile": "logs/node0.log" }
  ]
}
```

> P0 只有一个 `cluster.log` 时，`logFile` 统一填 `cluster.log`；P1 的单节点模式才产生 `logs/node{i}.log`。

## 7. 验收

在 VM 上、仓库根目录执行：

```bash
# 1. 起集群
python3 -m raftlab cluster up --nodes 3 --workdir runs/t2 --timeout 30
# 期望：打印 3 行节点表；退出码 0；总耗时 < 25s

# 2. 产物合法
python3 -m raftlab schema --check runs/t2/cluster.json
# 期望：OK

# 3. 端口真的通
python3 - <<'PY'
import json, socket
info = json.load(open("runs/t2/cluster.json"))
for n in info["nodes"]:
    socket.create_connection((n["ip"], n["port"]), 2).close()
print("ports ok")
PY

# 4. 状态
python3 -m raftlab cluster status --workdir runs/t2
# 期望：alive=3/3，退出码 0

# 5. 停
python3 -m raftlab cluster down --workdir runs/t2
pgrep -af raftCoreRun || echo "no leftover"
# 期望：no leftover

# 6. 连续 5 次 up/down 无残留、无端口冲突
for i in 1 2 3 4 5; do
  python3 -m raftlab cluster up --nodes 3 --workdir runs/t2-loop >/dev/null || break
  python3 -m raftlab cluster down --workdir runs/t2-loop >/dev/null || break
done
pgrep -af raftCoreRun || echo "loop clean"
```

补充失败路径：

```bash
python3 -m raftlab cluster up --nodes 3 --workdir runs/x --binary /nonexistent   # exit 2
python3 -m raftlab cluster up --nodes 3 --workdir runs/t2                        # 重复启动 exit 2
```

## 8. P1（可选加分）：单节点模式

**目的**：支持“只重启 node i”，并为每节点独立日志打基础。

### 8.1 C++ 改动（`example/raftCoreExample/raftKvDB.cpp`，约 20 行）

1. `getopt` 增加 `i:` 与 `p:`；
2. 仅当 `selfIndex < 0` 时截断 `test.conf`；
3. `selfIndex >= 0` 时：只创建一个 `KvServer(selfIndex, 500, configFileName, port)`，不 fork，`pause()` 后返回；`port` 优先取 `-p`，否则用原随机端口。

```cpp
int selfIndex = -1;
int fixedPort = 0;
// getopt "n:f:i:p:"
...
if (selfIndex >= 0) {
  short port = fixedPort > 0 ? static_cast<short>(fixedPort) : static_cast<short>(startPort);
  auto kvServer = new KvServer(selfIndex, 500, configFileName, port);
  pause();
  return 0;
}
```

> 必须支持 `-p`：重启时若端口变了，其他节点仍连着旧端口，集群不会恢复。

### 8.2 Python 侧

- `up` 改为逐个启动：`raftCoreRun -n N -i i -p <port> -f test.conf`，每个进程 `stdout=logs/node{i}.log`；
- 端口池由 M2 预分配（从 `basePort` 起找空闲端口）；
- `restart(index)`：停掉该 pid，用同一 `-p` 重新拉起，等待端口重新监听；
- `cluster.json` 的 `logFile` 改为 `logs/node{i}.log`。

### 8.3 P1 验收

```bash
python3 -m raftlab cluster restart --workdir runs/t2 --node 1
python3 -m raftlab cluster status --workdir runs/t2      # 仍 alive=3/3
```

## 9. 禁止事项

- 不要实现压测、故障注入、报告（分别属于 T3 / T4 / T7）。
- 不要引入第三方依赖（只用标准库；`psutil` 不允许）。
- 不要改 `raftlab/schema.py` 或 schema 文件来“适配实现”。
- 不要用 `pkill -f raftCoreRun` 这种全局杀进程的方式（会误杀别的 run）。
- 不要假设节点端口可预测，一律读 `test.conf`。

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 只杀父进程 | 子进程残留、端口占用 | `start_new_session=True` + `killpg` |
| 用 TCP 可连当就绪 | 立刻压测全是 `ErrWrongLeader` | 加 `startup_grace` |
| 在仓库根目录启动 | `test.conf` 写到根目录，多个 run 互相覆盖 | 强制 `cwd=workdir` |
| `test.conf` 追加写 | 重复 `up` 后出现重复条目 | 启动前删除旧 `test.conf` |
| 重启换端口 | 其他节点连不上，集群不恢复 | P1 必须支持 `-p` 复用端口 |
| 父进程 `pause()` | `wait()` 永不返回 | 不要 `wait()`，用轮询 + `killpg` |
