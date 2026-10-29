# RaftKV 测试平台 · 设计文档

> 注：代码已拆分到独立仓库 `raftlab/`，被测项目只保留 `kvbench` / `kvstatus` / `Status` RPC 等 C++ 侧改动（见 `docs/integration.md`）。本文档保留为设计记录。

> 被测系统：`KVstorageBaseRaft-cpp`（Raft + 跳表 KV）
> 目标：性能基准 + 故障/混沌测试 + 可复现实验记录 + 回归判定
> 形态：CLI 驱动的本地实验台（无服务端、无数据库）
> 版本：v0.1（2026-10）

---

## 目录

1. [逐块判断](#一逐块判断)
2. [右尺寸架构](#二右尺寸架构保留原图词汇压缩成-6-个模块)
3. [分阶段与工时](#三分阶段与工时)
4. [契约定义](#四契约定义)
5. [一次完整运行示例](#五一次完整运行示例)
6. [AI 并行开发的任务切分](#六ai-并行开发的任务切分)
7. [不做清单](#七不做清单)

---

## 一、逐块判断

原架构图是“平台级”方案：控制台 + 编排 + 4 个引擎 + 数据层，共 6 个子系统。对单人、数周的项目来说超出 2~3 倍，且其中两块性价比最低。

| 图中的块 | 判断 | 理由 |
|---|---|---|
| 统一控制台与实验管理 | **降级**：CLI + 静态 HTML 报告 | “管理集群 / 对比版本 / 追踪问题”本质是查询与展示；做 Web 控制台要多 1~2 周，静态报告能拿到 90% 的展示价值 |
| 实验编排与自动化控制 | **保留，作为骨架** | 实验生命周期、负载调度、故障编排、安全前置检查，是整个项目的主干 |
| 性能测试引擎 | **保留** | 吞吐 / 尾延迟 / 扩展性，是主线 |
| 故障与混沌测试引擎 | **保留，缩减动作集** | 宕机 / 分区 / STOP / 延迟留；磁盘异常砍（需 root 且污染宿主） |
| 正确性验证引擎 | **降级为冒烟** | 完整做（操作历史 + 一致性语义）等于另一个项目；只保留“故障后读校验” |
| 诊断与回归分析引擎 | **拆开** | “版本对比 / 修复验证”很便宜（manifest + diff）；“瓶颈分析 / 问题复现”昂贵，放 P2 |
| 统一数据采集与记录层 | **保留，用文件不用 DB** | `runs/<id>/` + JSON/JSONL 足够，不上时序数据库 |

---

## 二、右尺寸架构（保留原图词汇，压缩成 6 个模块）

### 2.1 全景图

```
                    scenario.yaml
                          │
              ┌───────────▼────────────┐
              │ M1 实验编排与安全前置   │  生命周期 / 时间轴 / seed / 清理
              └──┬────────┬────────┬───┘
        ┌────────▼──┐ ┌───▼─────┐ ┌▼──────────┐
        │ M2 集群管理 │ │ M3 性能  │ │ M4 混沌   │
        └────────┬──┘ └───┬─────┘ └┬──────────┘
                 └────────┼────────┘
                          ▼
              ┌───────────────────────┐
              │ M5 采集与实验记录层    │  metrics / events / logs / manifest
              └───────────┬───────────┘
                          ▼
              ┌───────────────────────┐
              │ M6 判定、回归与报告    │  SLO / 基线 diff / HTML
              └───────────┬───────────┘
                          ▼
                  （P2）M7 AI 诊断
```

### 2.2 六个模块

| 模块 | 对应原图 | 职责 | 对外契约 | 预估 |
|---|---|---|---|---|
| **M1 编排** | 实验编排与自动化控制 | 解析场景、按时间轴调度、并发后台任务、安全前置检查、异常清理 | 消费全部契约，产出 `run/` 目录 | 2d |
| **M2 集群** | 控制台中的“管理集群” | 生成配置、起停进程、端口发现、就绪探测、日志落盘 | `cluster.json` | 2d |
| **M3 性能** | 性能测试引擎 | 多线程客户端、负载模型、延迟直方图、吞吐统计 | `result.json` | 3d |
| **M4 混沌** | 故障与混沌引擎 | 故障动作、按时间点注入、时间线记录、恢复 | `events.json` | 2d |
| **M5 采集** | 统一数据采集与记录层 | `/proc` 采样、状态轮询、事件抽取、运行清单 | `metrics.jsonl` + `manifest.json` | 2d |
| **M6 判定/报告** | 诊断与回归（前半） | SLO 判定、可用性计算、基线 diff、HTML 报告 | `verdict.json` + `report.html` | 2d |
| （M7 AI） | 诊断与回归（后半） | 指标 + 事件 → 瓶颈假设 / 下轮实验建议 | `insights.md` | 2d，P2 |

**依赖规则**：M2~M4 互不 import，只通过 M1 编排；M2~M4 的输出文件由 M5 汇总；M6 只读文件。这是模块能被并行交给 agent 的前提。

### 2.3 一次实验的目录

```
runs/20261026-2110-baseline/
├── manifest.json     # 场景、seed、git commit、构建类型、硬件
├── cluster.json      # 节点 pid / 端口 / 配置
├── result.json       # 压测结果（吞吐 / 延迟分位 / 错误）
├── events.json       # 故障与状态事件时间线
├── metrics.jsonl     # 1s 粒度的资源与节点状态
├── logs/node0.log …  # 每个节点独立日志
├── verdict.json      # 判定结果与基线对比
└── report.html       # 单文件报告
```

`manifest.json` 记录 **git commit + Release/Debug + maxRaftState**，是“版本对比”成立的前提，成本几乎为零。

### 2.4 执行时序

```
M1 读 scenario
 ├─ 安全前置检查（端口 / 残留进程 / 磁盘 / root）
 ├─ M2 cluster.up ──► cluster.json
 ├─ 等待 leader（轮询 Status 或探测）
 ├─ M3 workload.start（后台线程）
 ├─ 到达 after 时间点 ──► M4 chaos.inject ──► events.json
 ├─ M3 workload.join ──► result.json
 ├─ M5 停止采样 ──► metrics.jsonl
 ├─ M6 计算 ──► verdict.json ──► report.html
 └─ M2 cluster.down（无论成败）
```

### 2.5 原图缺失、但必须补的三点

1. **测量有效性**：Release 构建、关闭 `DPrintf`、`maxRaftState` 参数化、单机口径说明。否则数字不可信。
2. **基线与 SLO 管理**：阈值存在场景里，regression 判定规则明确（例如 p99 退化 >20% 即失败）。
3. **安全前置的具体化**：端口占用、残留进程、磁盘空间、iptables 权限，任一失败立即中止并返回退出码 2。

---

## 三、分阶段与工时

| 阶段 | 内容 | 工期 | 验收标准 |
|---|---|---|---|
| **P0** | M1（顺序 step）+ M2 + M3（单并发、顺序读写）+ M6（文本判定） | 4~5 天 | 一条命令输出：吞吐、p99、kill leader 后的中断时长；退出码正确 |
| **P1** | 参数矩阵、M4（kill / stop / 分区）、M5（CPU / RSS / 事件）、M6（HTML + 基线 diff） | +5 天 | 报告含曲线、时间线、回归结论；同 seed 重跑结果可比 |
| **P2** | M7 AI 诊断、网络延迟/丢包、CI 集成 | +5 天 | 可选，不阻塞投递 |

里程碑：

- **M0（P0 结束）**：能证明“系统在故障下可用性是多少”。
- **M1（P1 结束）**：能证明“这次改动让性能退化/提升了多少”。
- **M2（P2 结束）**：能自动解释“为什么退化”。

---

## 四、契约定义

### 4.0 通用约定

| 约定 | 规定 |
|---|---|
| 时间戳 | UTC ISO-8601，毫秒精度，如 `2026-10-26T13:10:00.123Z` |
| 相对时间 | 整数 `tRelMs`，相对**本次运行开始**的单调时钟 |
| 时长（YAML） | 字符串带单位：`500ms`、`30s`、`2m` |
| 时长（JSON） | 整数毫秒，字段名以 `Ms` 结尾 |
| 吞吐 | `ops/s`，浮点 |
| 延迟 | 毫秒，浮点 |
| 容量 | 字节，整数；字段名以 `Bytes` 结尾 |
| 版本 | 每个文件必带 `schemaVersion`，semver 主版本不兼容 |
| 扩展字段 | 顶层禁止未知字段；`labels` / `detail` 内允许自由扩展 |

退出码：

| 码 | 含义 |
|---|---|
| 0 | 全部通过 |
| 1 | 断言失败（SLO / 可用性） |
| 2 | 环境或编排错误（起不来、端口占用、权限不足） |
| 3 | 超时 |

---

### 4.1 `scenario.yaml`

场景是一个声明式文件：**描述“怎么跑一次实验”**，不含实现细节。

```yaml
apiVersion: raftlab/v1
kind: Scenario
metadata:
  name: baseline-kill-leader
  description: 3 节点基线压测，第 10 秒 kill 掉 leader
  labels: {owner: me, suite: p1}

spec:
  seed: 42
  cluster:
    nodes: 3
    maxRaftState: 500      # KvServer 构造第二个参数
    build: Release         # Release | Debug
    logLevel: "off"        # 必须加引号：YAML 1.1 会把裸 off 解析成布尔 false
  defaults:
    timeout: 30s
    onError: abort         # abort | continue

  steps:
    - id: up
      action: cluster.up

    - id: ready
      action: wait.ready
      require: leader
      timeout: 20s

    - id: load
      action: workload.run
      loadId: L1
      duration: 30s
      warmup: 5s
      concurrency: 8
      mix: {put: 20, get: 70, append: 10}
      keySpace: 1000
      valueSize: 128
      background: true

    - id: kill
      action: chaos.inject
      fault: kill
      target: leader
      after: {ref: load, elapsed: 10s}
      timeout: 5s

    - id: join
      action: workload.wait
      ref: load
      timeout: 60s

    - id: avail
      action: assert.availability
      maxGapMs: 2000
      maxErrorRate: 0.05

    - id: down
      action: cluster.down
      onError: continue

  assertions:
    - metric: latency.p99
      op: "<="
      value: 100ms
    - metric: errors.rate
      op: "<="
      value: 0.02
```

#### 步骤通用字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 场景内唯一，供 `after.ref` 引用 |
| `action` | enum | 是 | 见下表 |
| `after` | int+单位 或 `{ref, elapsed}` | 否 | 相对上一步结束的延迟，或相对某后台步骤已运行时长 |
| `timeout` | duration | 否 | 默认取 `spec.defaults.timeout` |
| `onError` | `abort` / `continue` | 否 | 默认 `abort` |
| `background` | bool | 否 | 后台执行，不阻塞后续步骤 |

#### action 一览

| action | 参数 | 说明 |
|---|---|---|
| `cluster.up` | 取自 `spec.cluster` | 起集群并等待端口就绪 |
| `cluster.down` | — | 停止全部节点（幂等） |
| `cluster.restart` | `target` | 重启单个节点或全部 |
| `wait.ready` | `require: leader \| all_nodes`、`timeout` | 就绪判定 |
| `workload.run` | `loadId`、`duration`、`warmup`、`concurrency`、`mix`、`keySpace`、`valueSize`、`rate`（可选，ops/s）、`background` | 启动压测 |
| `workload.wait` | `ref`、`timeout` | 等待后台压测结束 |
| `chaos.inject` | `fault`、`target`、`duration`（部分故障） | 注入故障 |
| `assert.availability` | `maxGapMs`、`maxErrorRate` | 可用性断言 |
| `sleep` | `duration` | 显式等待 |

#### chaos 参数

| `fault` | `target` | 额外参数 | 说明 |
|---|---|---|---|
| `kill` | `leader` / `follower` / `node<N>` / `all` | — | `SIGKILL` |
| `stop` | 同上 | `duration` | `SIGSTOP`，到时自动 `SIGCONT` |
| `resume` | `node<N>` | — | 手动 `SIGCONT` |
| `restart` | `node<N>` | — | 重新拉起单节点 |
| `partition` | `node<N>` | `direction: in\|out\|both`、`duration` | iptables DROP |
| `delay` | `node<N>` | `latencyMs`、`duration` | `tc netem` |
| `loss` | `node<N>` | `percent`、`duration` | `tc netem` |

校验规则：

- `mix` 各项之和必须为 100；
- `after.ref` 必须指向已声明且 `background: true` 的步骤；
- `target: leader` 需要 M5 能提供 leader 信息，否则降级为“任一节点”并告警；
- `cluster.up` 之前不允许出现其他 action。

---

### 4.2 `manifest.json`

```json
{
  "schemaVersion": "1.0",
  "runId": "20261026-2110-baseline-kill-leader",
  "createdAt": "2026-10-26T13:10:00.123Z",
  "scenario": {
    "name": "baseline-kill-leader",
    "path": "scenarios/baseline-kill-leader.yaml",
    "sha256": "9f2c..."
  },
  "seed": 42,
  "host": {
    "hostname": "zz-VMware-Virtual-Platform",
    "os": "Ubuntu 24.04.4 LTS",
    "cpuCores": 2,
    "memTotalMB": 3863
  },
  "build": {
    "type": "Release",
    "gitCommit": "c11e580",
    "gitDirty": true,
    "compiler": "g++ 13.3.0",
    "protobuf": "3.21.12"
  },
  "cluster": {
    "nodes": 3,
    "maxRaftState": 500,
    "logLevel": "off",
    "basePort": 29016
  },
  "tool": {"name": "raftlab", "version": "0.1.0"}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemaVersion` | string | 是 | 固定 `1.0` |
| `runId` | string | 是 | `YYYYMMDD-HHMM-<场景名>` |
| `createdAt` | string | 是 | UTC 毫秒 |
| `scenario.sha256` | string | 是 | 场景文件哈希，防“改了场景还比旧结果” |
| `seed` | int | 是 | 复现用 |
| `host` | object | 是 | 硬件口径 |
| `build.type` | `Release`/`Debug` | 是 | 结果可比性关键 |
| `build.gitCommit` | string | 是 | 版本对比关键 |
| `cluster.maxRaftState` | int | 是 | 快照阈值，直接影响性能 |

---

### 4.3 `cluster.json`

```json
{
  "schemaVersion": "1.0",
  "configFile": "runs/20261026-2110-baseline-kill-leader/test.conf",
  "startedAt": "2026-10-26T13:10:01.000Z",
  "readyAt": "2026-10-26T13:10:11.200Z",
  "nodes": [
    {"index": 0, "pid": 4101, "ip": "127.0.1.1", "port": 29016, "logFile": "logs/node0.log"},
    {"index": 1, "pid": 4102, "ip": "127.0.1.1", "port": 29017, "logFile": "logs/node1.log"},
    {"index": 2, "pid": 4103, "ip": "127.0.1.1", "port": 29018, "logFile": "logs/node2.log"}
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `nodes[].pid` | int | 进程号，故障注入直接使用 |
| `nodes[].port` | int | 从 `test.conf` 反查 |
| `readyAt` | string | 首次探测到 leader 的时间 |

---

### 4.4 `result.json`

```json
{
  "schemaVersion": "1.0",
  "runId": "20261026-2110-baseline-kill-leader",
  "loadId": "L1",
  "startedAt": "2026-10-26T13:10:12.000Z",
  "endedAt": "2026-10-26T13:10:42.000Z",
  "durationMs": 30000,
  "warmupMs": 5000,
  "config": {
    "concurrency": 8,
    "mix": {"put": 20, "get": 70, "append": 10},
    "keySpace": 1000,
    "valueSize": 128,
    "rateLimit": null
  },
  "counts": {
    "total": 123456,
    "ok": 123000,
    "error": 456,
    "byOp": {"get": 86000, "put": 25000, "append": 12456}
  },
  "errors": {
    "rate": 0.0037,
    "byType": {"timeout": 400, "conn_reset": 56}
  },
  "throughput": {
    "total": 4115.2,
    "byOp": {"get": 2866.7, "put": 833.3, "append": 415.2}
  },
  "latencyMs": {
    "all": {"p50": 1.2, "p90": 3.4, "p95": 5.0, "p99": 12.3, "p999": 40.1, "max": 120.0},
    "get": {"p50": 0.9, "p99": 8.1},
    "put": {"p50": 2.1, "p99": 18.4},
    "append": {"p50": 2.3, "p99": 20.2}
  },
  "notes": ["closed-loop client; coordinated omission not corrected"]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `durationMs` | int | 不含 warmup |
| `counts.ok` | int | 收到成功响应的操作数 |
| `errors.byType` | object | 见错误分类 |
| `latencyMs.*` | object | 分位数，单位 ms |
| `notes` | string[] | 口径声明，例如是否开环 |

错误分类（`errors.byType` 的 key）：

| key | 含义 |
|---|---|
| `timeout` | 超过 `CONSENSUS_TIMEOUT` 或客户端超时 |
| `conn_reset` | 连接被重置 / 对端关闭 |
| `wrong_leader` | 返回 `ErrWrongLeader` 且重试耗尽 |
| `rpc_parse` | 响应反序列化失败 |
| `other` | 其他 |

---

### 4.5 `events.json`

```json
{
  "schemaVersion": "1.0",
  "runId": "20261026-2110-baseline-kill-leader",
  "events": [
    {"ts": "2026-10-26T13:10:22.000Z", "tRelMs": 10000, "source": "chaos",
     "type": "fault.inject", "target": "node0",
     "detail": {"fault": "kill", "pid": 4101}},

    {"ts": "2026-10-26T13:10:22.010Z", "tRelMs": 10010, "source": "cluster",
     "type": "node.down", "target": "node0"},

    {"ts": "2026-10-26T13:10:22.430Z", "tRelMs": 10430, "source": "telemetry",
     "type": "leader.change", "target": "node1",
     "detail": {"from": 0, "to": 1, "term": 4}},

    {"ts": "2026-10-26T13:10:22.812Z", "tRelMs": 10812, "source": "workload",
     "type": "workload.first_success_after_fault", "target": "L1"},

    {"ts": "2026-10-26T13:10:32.000Z", "tRelMs": 20000, "source": "telemetry",
     "type": "snapshot.created", "target": "node1",
     "detail": {"index": 1280, "term": 4}}
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `tRelMs` | int | 相对运行开始，**用于时间线对齐** |
| `source` | enum | `orchestrator` / `cluster` / `chaos` / `workload` / `telemetry` |
| `type` | string | 点分命名，见事件类型表 |
| `target` | string | `node<N>` / `leader` / `L1` |
| `detail` | object | 自由扩展 |

事件类型（v1 必需）：

| type | 产生者 | 用途 |
|---|---|---|
| `fault.inject` / `fault.recover` | M4 | 故障窗口边界 |
| `node.down` / `node.up` | M2 | 进程状态 |
| `leader.change` | M5 | 选主耗时 |
| `snapshot.created` | M5 | 快照频率（性能归因） |
| `workload.first_error_after_fault` | M3 | 检测时延 |
| `workload.first_success_after_fault` | M3 | 恢复时延 |

---

### 4.6 `metrics.jsonl`

每行一个采样点，1s 粒度：

```json
{"ts":"2026-10-26T13:10:13.000Z","tRelMs":1000,"node":0,"cpuPct":45.2,"rssMB":120.3,"term":3,"isLeader":true,"commitIndex":120,"logSize":120,"snapshotIndex":0}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `node` | int | 节点下标 |
| `cpuPct` | float | 该进程 CPU 占用（`ps` 采样） |
| `rssMB` | float | 常驻内存 |
| `term` / `isLeader` / `commitIndex` / `logSize` / `snapshotIndex` | — | 来自 `Status` RPC；不可得时为 `null` |
| `sampled` | bool | 可选，标记数据是否来自降级解析 |

---

### 4.7 `verdict.json`

```json
{
  "schemaVersion": "1.0",
  "runId": "20261026-2110-baseline-kill-leader",
  "status": "fail",
  "checks": [
    {"id": "slo.latency.p99", "type": "slo",
     "metric": "latency.p99", "op": "<=", "threshold": 100.0, "actual": 132.4,
     "passed": false},
    {"id": "slo.errors.rate", "type": "slo",
     "metric": "errors.rate", "op": "<=", "threshold": 0.02, "actual": 0.0037,
     "passed": true},
    {"id": "availability.maxGapMs", "type": "availability",
     "metric": "availability.maxGapMs", "op": "<=", "threshold": 2000, "actual": 812,
     "passed": true}
  ],
  "summary": {"passed": 2, "failed": 1},
  "baseline": {
    "runId": "20261025-2010-baseline",
    "deltas": {"throughput.total": -12.3, "latency.p99": 8.1}
  },
  "artifacts": {
    "report": "report.html",
    "events": "events.json",
    "metrics": "metrics.jsonl"
  }
}
```

| 字段 | 说明 |
|---|---|
| `status` | `pass` / `fail` / `error` |
| `checks[].op` | `<=` `>=` `<` `>` `==` |
| `baseline.deltas` | 相对基线的百分比变化，正负号表示方向 |
| `artifacts` | 相对 `runs/<id>/` 的路径 |

---

### 4.8 指标命名表

| 指标 | 单位 | 说明 |
|---|---|---|
| `throughput.total` | ops/s | 总吞吐（不含 warmup） |
| `throughput.byOp.<op>` | ops/s | 分操作吞吐 |
| `latency.p50/p90/p95/p99/p999` | ms | 全量分位 |
| `latency.byOp.<op>.p99` | ms | 分操作尾延迟 |
| `errors.rate` | ratio | 错误数 / 总数 |
| `availability.maxGapMs` | ms | 故障期间最长“无成功响应”窗口 |
| `availability.detectMs` | ms | 注入到首个错误的时延 |
| `availability.recoveryMs` | ms | 注入到恢复稳定成功的时延 |
| `resources.cpuPct.max` | % | 单节点 CPU 峰值 |
| `resources.rssMB.max` | MB | 单节点内存峰值 |

---

## 五、一次完整运行示例

```bash
$ raftlab run scenarios/baseline-kill-leader.yaml --seed 42

[0] preflight ................ ok    ports free, 3.1G disk, root=no
[1] cluster.up(3) ............ ok    11.2s  ports 29016-29018
[2] wait.ready(leader) ....... ok    leader=node0 term=3
[3] workload L1 .............. running  8 conns, 30s
[4] chaos.kill(node0) ........ ok    t=10.0s
[5] workload L1 .............. done  123456 ops, 4115 ops/s
[6] availability ............. ok    gap=812ms, err=0.37%
[7] verdict .................. FAIL  p99=132.4ms > 100ms
[8] report ................... runs/20261026-2110-baseline-kill-leader/report.html

exit code: 1
```

---

## 六、AI 并行开发的任务切分

| 任务单 | 模块 | 交付 | 验收命令 |
|---|---|---|---|
| T1 `contracts` | — | 5 个 schema + 校验器 | `python -m raftlab.schema --check runs/demo` |
| T2 `cluster` | M2 | `cluster.json` | `raftlab cluster up --nodes 3 && jq . nodes` |
| T3 `bench` | M3 | `result.json` | `raftlab bench --duration 10s --concurrency 8` |
| T4 `chaos` | M4 | `events.json` | `raftlab chaos kill --target leader` |
| T5 `telemetry` | M5 | `metrics.jsonl` | 采样 10s，行数 ≈ 10×节点数 |
| T6 `orchestrator` | M1 | 端到端跑通 T2~T5 | `raftlab run scenarios/baseline.yaml` |
| T7 `verdict/report` | M6 | `verdict.json` + `report.html` | 退出码与 `status` 一致 |

规则：

- 每个任务 ≤ 300 行改动，禁止跨模块修改；
- 只依赖 schema，不依赖别人的内部函数；
- 合并前必须通过 `schema --check` 与冒烟命令。

---

## 七、不做清单

- 完整的线性一致性验证（只保留故障后读校验）
- 实时 Web 监控台（只做静态 HTML）
- 数据库/时序库（用文件）
- K8s / 多机编排
- 通用化到“支持任意被测系统”
- 自动修复缺陷
