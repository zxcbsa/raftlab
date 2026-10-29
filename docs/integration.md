# 被测项目需要的改动

`raftlab` 是黑盒工具，但仍然需要被测项目提供三样东西：**状态接口**、**压测客户端**、**可控的单节点启动**。以下是 `KVstorageBaseRaft-cpp` 上已经落地的改动，可作为移植到其他 Raft KV 的参考。

## 1. Status RPC（只读状态）

`src/raftRpcPro/kvServerRPC.proto`：

```proto
message StatusArgs {}
message StatusReply {
  int32 Term = 1;
  bool IsLeader = 2;
  int32 CommitIndex = 3;
  int32 LastApplied = 4;
  int32 LogSize = 5;
  int32 SnapshotIndex = 6;
  int32 LeaderId = 7;
}
service kvServerRpc {
  rpc PutAppend(PutAppendArgs) returns(PutAppendReply);
  rpc Get (GetArgs) returns (GetReply);
  rpc Status (StatusArgs) returns (StatusReply);
}
```

实现要点：

- `Raft::GetStatus()` 在**一次加锁**内读六个字段，避免多次取值不一致；
- `KvServer::Status()` 直接读 Raft，**不经过日志**，不会影响写路径；
- 重新生成 pb 时 protoc 版本必须与运行时一致（本项目 3.21.12）。

## 2. `kvstatus`：状态查询 CLI

`example/raftCoreExample/kvstatus.cpp`：读 `test.conf` 的节点列表，逐个发 `Status`，输出 JSON Lines：

```json
{"index":0,"ok":true,"term":3,"isLeader":true,"commitIndex":120,...}
```

节点不可达时输出 `{"index":2,"ok":false}`，整体退出码仍为 0。`raftlab` 用它做 leader 判定、`chaos --target leader`、`wait.ready`。

> 注意：RPC 层的 `DPrintf` 会往 stdout 打印日志，解析方只取以 `{` 开头的行。

## 3. `kvbench`：多线程压测客户端

`example/raftCoreExample/kvbench.cpp` 的要点：

- 每个线程独立 `clientId`，`raftServerRpcUtil` 每 peer 一个；
- 每次逻辑操作最多尝试 N 个节点，**失败后退避 2ms**（否则选举期间的高频重试会饿死选举）；
- 延迟按逻辑操作统计（含重试），输出 p50/p90/p95/p99/p999/max；
- 输出必须符合 `result.schema.json`。

## 4. `-i / -p / -m`：单节点启动

`example/raftCoreExample/raftKvDB.cpp`：

| 参数 | 含义 |
|---|---|
| `-i <index>` | 只启动指定节点，不 fork；**不能截断 `test.conf`** |
| `-p <port>` | 固定端口，重启必须复用，否则其他节点连不上 |
| `-m <n>` | `maxRaftState`，快照阈值，压测时必须可调 |

端口必须小于 32768（`KvServer` / `RpcProvider` 用有符号 `short`）。

## 5. RPC 层两处必要加固

`src/rpc/mprpcchannel`：

- 增加可选超时（`MprpcChannel(ip, port, connectNow, timeoutMs)`），否则节点被 `SIGSTOP` 时客户端永久阻塞；
- `CallMethod` 用互斥量串行化——Raft 的心跳与选举线程会并发调用同一个 peer，否则会读到对方的响应。

`RaftRpcUtil` 使用 3s 超时。

## 6. Raft 初始化守卫

`Raft::init()` 完成前，`m_persister` 为空。若此时收到 `RequestVote`/`AppendEntries`，会 `nullptr` 解引用直接段错误（per-node 启动和单节点重启必现）。

```cpp
std::atomic<bool> m_initialized{false};   // 成员
// 三个 RPC handler 入口：
if (!m_initialized.load()) { reply->set_term(0); return; }
// init() 末尾：
m_initialized.store(true);
```

## 7. 构建开关

```cmake
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE "Debug")
endif()
option(RAFT_DEBUG_LOG "Enable DPrintf logging" ON)
if(NOT RAFT_DEBUG_LOG)
  add_compile_definitions(RAFT_DEBUG_LOG=0)
endif()
```

跳表的逐操作 `cout` 同样要用 `RAFT_DEBUG_LOG` 关掉，否则 6 秒能产生 25MB 日志。

## 8. 兼容性检查清单

```bash
# 三个产物都存在
ls bin/raftCoreRun bin/kvbench bin/kvstatus

# 状态接口可用
./bin/kvstatus --config runs/t2/test.conf | grep '^{'

# 单节点模式可用
./bin/raftCoreRun -n 3 -i 0 -p 25000 -m 5000 -f test.conf
```
