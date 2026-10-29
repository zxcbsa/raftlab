# raftlab

`raftlab` 是 Raft KV 集群的实验台：**场景 → 执行 → 数据 → 判定 → 报告**，同一场景 + 同一 seed 必须可复现。

当前进度：

| 任务 | 模块 | 状态 |
|---|---|---|
| T1 | 契约层（7 份 JSON Schema + 校验器） | ✅ |
| T2 | 集群管理（起停 / 状态 / 整集群重启 / 单节点重启） | ✅ P0+P1 |
| T3 | 压测（`kvbench` + `result.json`） | ✅ P0 |
| T4 | 混沌（kill / stop / resume / partition） | ✅ P0 |
| T5 | 状态与采集（`Status` RPC + `kvstatus` + `metrics.jsonl`） | ✅ |
| T6 | 场景编排（`raftlab run` + manifest/verdict） | ✅ |
| T7 | HTML 报告、基线对比 | ✅ |

## 实测（Ubuntu 24.04 / 2C 4G / Release + 关日志）

| 场景 | 结果 | 吞吐 | p99 | 故障窗口 | 失败率 |
|---|---|---|---|---|---|
| `baseline-kill-leader`（3 节点，c=4，第 10s 杀 leader） | PASS | 164 ops/s | 35 ms | 1.25 s | 9% |
| `restart-node`（per-node，杀 node0 后重启） | PASS | 166 ops/s | 30 ms | 1.2 s | 12% |

失败全部是选主窗口内的 `ErrWrongLeader`；客户端失败后 2ms 退避，窗口约 1.2s。阈值按这个量级设置（见场景文件里的注释）。

## 依赖

```bash
sudo apt install -y python3-yaml python3-jsonschema
```

压测客户端需要先编译。**做性能测试时必须用 Release + 关闭日志**，否则测到的是 `printf`：

```bash
# 正确性 / 调试
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j

# 性能测试（产物同样输出到仓库 bin/，会覆盖上一份）
cmake -S . -B build-perf -DCMAKE_BUILD_TYPE=Release -DRAFT_DEBUG_LOG=OFF
cmake --build build-perf -j
```

> 基准数字只在 Release + `RAFT_DEBUG_LOG=OFF` 下有意义；Debug 构建下请只看“是否跑通”。

`chaos partition` 需要 root 且不能交互输入密码（`sudo -n`）。二选一：

```bash
# 方案 A：直接以 root 运行
sudo -i
# 方案 B：给 iptables 配 NOPASSWD
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/iptables" | sudo tee /etc/sudoers.d/raftlab-iptables
```

## 用法

```bash
# 0. 一条命令跑完整场景（起集群 → 压测 → 故障 → 采集 → 判定 → 报告）
python3 -m raftlab run scenarios/baseline-kill-leader.yaml

# 与基线对比：p99 退化 >20% 或吞吐下降 >20% 判为失败
python3 -m raftlab run scenarios/baseline-kill-leader.yaml --baseline runs/上一次的run

# 单独重出报告（可带基线）
python3 -m raftlab report --run runs/xxx --baseline runs/yyy

# 也可以指定输出目录 / 覆盖 seed
python3 -m raftlab run scenarios/baseline-kill-leader.yaml --run-dir runs/my-exp --seed 7

# 1. 起集群（3 节点，workdir 里会生成 test.conf / cluster.json / cluster.log）
python3 -m raftlab cluster up --nodes 3 --workdir runs/t2

# 需要单独重启某个节点时，用 per-node 模式（每节点独立进程 + 独立日志）
python3 -m raftlab cluster up --nodes 3 --workdir runs/t2pn --mode per-node

# 2. 看状态
python3 -m raftlab cluster status --workdir runs/t2

# 3. 压测 10 秒（8 并发，20/70/10 的 put/get/append 混合）
python3 -m raftlab bench --workdir runs/t2 --duration 10 --concurrency 8

# 4. 注入故障（--target 支持 nodeN 或 leader）
python3 -m raftlab chaos kill   --workdir runs/t2 --target leader
python3 -m raftlab chaos stop   --workdir runs/t2 --target node1 --duration-ms 3000
python3 -m raftlab chaos resume --workdir runs/t2 --target node1
python3 -m raftlab chaos restart --workdir runs/t2pn --target node1   # 需要 per-node 模式

# 5. 采集状态与资源（1s 一次，写入 metrics.jsonl）
python3 -m raftlab telemetry --workdir runs/t2 --duration 15 --interval 1

# 6. 停集群
python3 -m raftlab cluster down --workdir runs/t2

# 7. 校验产物
python3 -m raftlab schema --check runs/t2
```

`chaos --target leader` 依赖 `bin/kvstatus`（通过 `Status` RPC 查询当前 leader）；节点全部不可达时会报错。

退出码：`0` 通过，`1` 运行期失败，`2` 用法/环境错误。

> 注意：YAML 1.1 会把裸写的 `off` 解析成布尔 `false`，场景里的 `logLevel` 必须写成 `"off"`。
> `chaos` 的 `--target leader` 暂不支持——leader 判定依赖 T5 的 `Status` RPC，目前请用 `nodeN`。

## 两种启动模式

| 模式 | 进程模型 | 日志 | 能单独重启节点 |
|---|---|---|---|
| `fork`（默认） | 一个 `raftCoreRun` fork N 个节点 | `cluster.log` | ❌ |
| `per-node` | 每节点一个 `raftCoreRun -i i -p <端口>` | `logs/node{i}.log` | ✅ |

`per-node` 模式由 M2 预先分配端口（bind 0 探测），重启时把**原端口**传给 `-p`，否则其他节点仍连着旧端口、集群恢复不了。示例见 `scenarios/restart-node.yaml`（压测中 kill node0，再把它拉起来）。

## 目录

```
raftlab/
├── cli.py         # schema / cluster / bench / chaos / telemetry 子命令
├── cluster.py     # 起停进程组、解析 test.conf、写 cluster.json
├── workload.py    # 调 kvbench，校验 result.json
├── chaos.py       # kill / stop / resume / partition + events.json
├── telemetry.py   # 调 kvstatus + 采样 /proc，写 metrics.jsonl
├── orchestrator.py# 场景执行：步骤调度、manifest、verdict
├── evaluator.py   # 断言、指标聚合、verdict.json
├── report.py      # 单文件 HTML 报告（无外部依赖）
├── events.py      # 事件时间线
├── schema.py      # Schema 加载、JSON 校验、跨文件语义检查
└── schemas/       # 7 份契约（唯一事实来源）
scenarios/         # 实验场景
tests/             # 单元测试与夹具
```

## 一次实验的产物

```
runs/t2/
├── cluster.json   # 节点 pid / 端口 / 配置
├── cluster.log    # 三个节点共用的 stdout（P0 用 fork 模型）
├── result.json    # 吞吐 / 延迟分位 / 错误
├── events.json    # 故障时间线
└── test.conf      # 端口发现
```

场景运行还会额外产出 `manifest.json`（场景哈希、seed、构建、硬件）、`verdict.json`（判定结果）与 `report.html`（单文件报告：指标卡、延迟分布、CPU 曲线、断言表、事件时间线、基线对比）。

**`availability.maxGapMs` 的口径**：从第一个 `fault.inject` 到其后的 `leader.change`（或 `fault.recover`）的时间差，即“故障到重新可用”的窗口；没有故障时为 0。这是可观测的近似，不等于严格意义上的“无成功请求时长”。

## 跨文件语义检查（JSON Schema 表达不了的部分）

| 检查 | 说明 |
|---|---|
| `runId` 一致 | manifest / result / events / verdict 必须相同 |
| `counts` 自洽 | `ok + error == total` |
| 延迟单调 | `p50 <= p90 <= p95 <= p99 <= p999 <= max` |
| 事件有序 | `events[].tRelMs` 非递减 |
| 判定自洽 | `status` 与 `checks[].passed`、`summary.failed` 一致 |
| 节点范围 | `metrics[].node` 必须小于 `manifest.cluster.nodes` |
| 场景合法 | 首个步骤必须是 `cluster.up`；`mix` 之和为 100；`after.ref` 必须指向更早的 `background` 步骤 |

## 自测

```bash
bash tests/selftest.sh
```

或分别执行：

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m raftlab schema --check tests/fixtures/valid-run
python3 -m raftlab schema --check tests/fixtures/invalid-run --allow-missing   # 期望 1
python3 -m raftlab schema --check tests/fixtures/bad-scenario.yaml             # 期望 1
```
