# raftlab

面向 Raft KV 集群的**性能 + 故障实验台**：一条命令完成「起集群 → 加压 → 注入故障 → 采集 → 判定 → 出报告」，同一场景 + 同一 seed 可复现。

它不是单元测试框架，而是黑盒实验工具：通过进程、信号、iptables 施加故障，通过 `Status` RPC 和 `/proc` 观测状态，用 JSON 契约保证结果可比。

## 能力

| 模块 | 能力 |
|---|---|
| 集群管理 | `fork` / `per-node` 两种模式，起停、状态、整集群与单节点重启 |
| 压测 | 多线程、读写混合、延迟分位（p50~p999）、错误分类 |
| 混沌 | `kill` / `stop` / `resume` / `restart` / `partition` |
| 采集 | 1s 粒度的 CPU/RSS + term/角色/commitIndex/logSize/snapshotIndex |
| 判定 | SLO 断言、可用性窗口、基线回归（p99 退化 >20% 或吞吐下降 >20% 判失败） |
| 报告 | 单文件 HTML，无 CDN、无外部依赖 |
| 契约 | 7 份 JSON Schema + 跨文件语义校验 |

## 快速开始

### 1. 准备被测项目

被测项目是 [`KVstorageBaseRaft-cpp`](https://github.com/youngyangyang04/KVstorageBaseRaft-cpp)，需要先补齐 `Status` RPC、`-i/-p/-m` 参数和 `kvbench`/`kvstatus`（见 `docs/integration.md`），然后编译：

```bash
# 正确性 / 调试
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j

# 性能实验（关闭 DPrintf，否则测到的是 printf）
cmake -S . -B build-perf -DCMAKE_BUILD_TYPE=Release -DRAFT_DEBUG_LOG=OFF
cmake --build build-perf -j
```

产物：`bin/raftCoreRun`、`bin/kvbench`、`bin/kvstatus`。

### 2. 安装 raftlab

```bash
sudo apt install -y python3-yaml python3-jsonschema
# 或者：pip install -e .
```

### 3. 指向被测项目并运行

```bash
export RAFTLAB_TARGET=~/KVstorageBaseRaft-cpp-main   # 或每条命令加 --project

python3 -m raftlab run scenarios/baseline-kill-leader.yaml
```

输出：

```
[raftlab] run 20261029-1729-baseline-kill-leader -> runs/...
PASS  20261029-1729-baseline-kill-leader
  ok   availability.maxGapMs: 1245.00 <= 2000.00
  ok   errors.rate: 0.09 <= 0.10
  ok   latency.p99: 35.40 <= 100.00
  report: runs/.../report.html
```

## 命令速查

```bash
# 契约校验
python3 -m raftlab schema --check runs/xxx
python3 -m raftlab schema --check scenarios/baseline-kill-leader.yaml

# 集群
python3 -m raftlab cluster up     --nodes 3 --workdir runs/t2 [--mode per-node] [--max-raft-state 50000]
python3 -m raftlab cluster status --workdir runs/t2
python3 -m raftlab cluster down   --workdir runs/t2

# 压测 / 采集 / 故障
python3 -m raftlab bench     --workdir runs/t2 --duration 10 --concurrency 4
python3 -m raftlab telemetry --workdir runs/t2 --duration 10
python3 -m raftlab chaos kill    --workdir runs/t2 --target leader
python3 -m raftlab chaos stop    --workdir runs/t2 --target node1 --duration-ms 3000
python3 -m raftlab chaos restart --workdir runs/t2 --target node1   # 需要 per-node 模式

# 场景 + 报告 + 基线
python3 -m raftlab run scenarios/baseline-kill-leader.yaml
python3 -m raftlab run scenarios/restart-node.yaml --baseline runs/上一次
python3 -m raftlab report --run runs/xxx [--baseline runs/yyy]
```

## 一次实验的产物

```
runs/20261029-1729-baseline-kill-leader/
├── manifest.json    # 场景哈希、seed、git commit、构建类型、硬件
├── cluster.json     # 节点 pid / 端口 / 模式 / maxRaftState
├── cluster.log      # fork 模式合并日志（per-node 模式为 logs/node{i}.log）
├── result.json      # 吞吐、延迟分位、错误分类
├── events.json      # 故障与选主时间线
├── metrics.jsonl    # 1s 粒度资源与状态
├── verdict.json     # 判定结果与基线对比
└── report.html      # 单文件报告
```

跑完会自动用 schema 校验产物；不合契约时退出码为 2。

## 实测（Ubuntu 24.04 / 2C 4G / Release + 关日志）

| 场景 | 结果 | 吞吐 | p99 | 故障窗口 | 失败率 |
|---|---|---|---|---|---|
| `baseline-kill-leader`（3 节点，c=4，第 10s 杀 leader） | PASS | 164 ops/s | 35 ms | 1.25 s | 9% |
| `restart-node`（per-node，杀 node0 后重启） | PASS | 166 ops/s | 30 ms | 1.2 s | 12% |

失败全部集中在选主窗口内的 `ErrWrongLeader`。客户端失败后 2ms 退避，窗口约 1.2s；阈值按这个量级设置，不是硬凑。

## 目录

```
raftlab/            # Python 包（CLI / 集群 / 压测 / 混沌 / 采集 / 编排 / 判定 / 报告）
scenarios/          # 场景 DSL
tests/              # 单元测试 + 契约夹具 + selftest.sh
tasks/              # 任务单（T2 等）
docs/               # 设计文档、集成说明、问题复盘
```

## 已知限制

- 目前只支持单机多进程，绝对吞吐没有意义，只做相对比较。
- `availability.maxGapMs` 是「第一次故障注入 → 之后首次 leader 变更/恢复」的近似，不是严格的无成功请求窗口。
- `partition` 依赖 `sudo -n iptables`（或 root）。
- 单节点重启依赖被测项目的 `-i/-p` 补丁与 per-node 模式。

## 文档

- [设计文档](docs/design.md)：契约、模块划分、分阶段计划
- [集成说明](docs/integration.md)：被测项目需要做的改动
- [问题复盘](docs/findings.md)：在真机上发现并修复的 10 个问题
