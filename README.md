# Cloud Bench

**云服务器性能评测工具** —— 一套可复现的深度评测脚本。

> 本仓库**只放工具**：评测脚本 + 方法论 + 踩坑记录。
> 数据集与对比站点由运营方私有维护，不在本仓库（详见文末「仓库边界」）。

## 为什么不用现成的

YABS、bench.sh、融合怪很快很好用，但它们测的是"快不快"，不测"稳不稳"。
实测下来这几处盲区恰恰决定实际体感：

| 盲区 | 后果 | 本工具 |
|---|---|---|
| 只有平均 IOPS，无长尾延迟 | 平均 26000 IOPS 看着漂亮，p99 可能是 **952ms**——数据库/容器的卡顿就来自这里 | fio 记录 **p99 / p99.9** |
| 几乎不做超售检测 | 邻居跑满时你的机器被抢 CPU，跑分那几分钟看不出来 | stress-ng 满载 20 分钟 + mpstat 持续记 **%steal** |
| 只看标称核数 | "4 核"可能是 2 物理核 × 2 超线程 | 拆出 **物理核/线程**；多核扩展比作旁证 |
| 不检测指令集 | 屏蔽 AVX 的机器跑某些负载会直接 SIGILL | AVX / AVX-512 检测 |
| 无原始数据 | 只有截图，无法复核 | 原始产物全量保留，汇总可重算 |

**不是"更准"，而是多测了几个维度**——跑分同量级时，这些维度才决定体验。

## 快速开始

```bash
git clone https://github.com/holtwood-projects/cloud-bench.git
cd cloud-bench

# 1. 先在 ~/.ssh/config 里配好目标机器的别名（并确保免密登录可用）
# 2. 复制清单模板，按自己的机器填写
cp results/hosts.tsv.example results/hosts.tsv
vim results/hosts.tsv

# 3. 开跑（默认 quick 档按历史推算约 16 分钟，待真机验收；远端 tmux 或 setsid+nohup）
./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01

#    想降低单次波动：--repeat 3 会顺序跑 3 个独立 run，总耗时约 ×3
./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --repeat 3

# 4. 看进度 / 回收产物
#    每次 run 会生成独立 run_id，产物在 results/<name>/<run_id>/
#    status/collect 默认取最新一次；要指定旧运行加 --run <ID>
./scripts/cloud-benchmark.sh status  --name mycloud-2c2g-01
./scripts/cloud-benchmark.sh collect --name mycloud-2c2g-01

#    批次模式：一次看/收整批样本
./scripts/cloud-benchmark.sh status  --name mycloud-2c2g-01 --batch <batch_id>
./scripts/cloud-benchmark.sh collect --name mycloud-2c2g-01 --batch <batch_id>

# 5. 出一份「人话报告」——会不会卡 / 有没有虚标 / 能跑什么
./scripts/cloud-benchmark.sh report  --name mycloud-2c2g-01
./scripts/cloud-benchmark.sh report  --name mycloud-2c2g-01 --batch <batch_id>
```

### 三档耗时

| 档位 | 内容 | 耗时 |
|---|---|---|
| **`quick`（默认）** | 磁盘 / CPU / 内存 / 网络 / 超售检测 | **约 16 分钟（推算，待验收）** |
| `standard` | quick + YABS 交叉验证 + PTS（7-Zip、redis） | 约 40 分钟 |
| `full` | 全量，含 PTS 内核编译 | 约 2.5 小时 |

**为什么默认目标是 16 分钟**：轻量云用户不会在 2C2G 上编译内核，也不关心 openssl 跑分——
这类通用跑分项恰恰是最耗时的（内核编译单项就占全量的 59%）。
砍掉它们之后，**全部差异化指标一个不少**：

```
磁盘 p99 长尾  ← fio      （决定"卡不卡"）
超售 %steal    ← stress   （决定"稳不稳"）
物理核识别     ← env      （识破"超线程当核卖"）
```

按历史日志推算约 16 分钟就能拿到 YABS / bench.sh 那类脚本测不出的数据——这是设计目标，仍需真机验收。

常用参数：

```bash
--profile standard|full   # 换档位
--stages env,deps,fio     # 只跑指定阶段（逐项覆盖档位预设）
--stress-mins 20          # 压测时长（quick 档默认 5 分钟）
--pts-tests "compress-7zip redis"  # 显式启用 pts 时必填，quick 没有默认 PTS 列表
--at 23:30                # 在**远端**定时开跑（跑生产机时用来卡低峰窗口）
--run <ID>                # status/collect/report/submit 指定某次运行
--batch <ID>              # status/collect/report/submit/summarize 指定批次
--repeat N                # run 时顺序执行 N 次独立采样（N=1 保持旧行为）
--force                   # run 中断旧会话；collect 覆盖已有结果（保留 .bak）
```

> 每次运行都有独立的 `run-status.tsv` / `stage-status.tsv`；失败阶段显示 `failed`，
> 部分完成显示 `partial`（例如 PTS 只跑出了部分测试），不会再出现「阶段全跑完、
> 报告却用了上次数据」的情况。

### 多次采样：`--repeat` 与 `--batch`

云盘和公网性能都有波动，单次结果不足以判断稳定性。`--repeat N` 会为同一台机器
生成 N 个独立 `run_id`，在远端**顺序**执行（不并发，避免互相污染），并写批次清单：

```bash
./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --repeat 3
# 输出批次 ID 后：
./scripts/cloud-benchmark.sh status  --name mycloud-2c2g-01 --batch 20261106T000000Z-abc123
./scripts/cloud-benchmark.sh collect --name mycloud-2c2g-01 --batch 20261106T000000Z-abc123
./scripts/cloud-benchmark.sh report  --name mycloud-2c2g-01 --batch 20261106T000000Z-abc123
```

- 每批仍是 `results/<name>/<run_id>/`，批次清单落在
  `results/<name>/batches/<batch_id>.tsv`；旧的单 run 目录与 `LATEST` 完全不变。
- `collect --batch` 会先检查整批：任一样本仍在 `starting/running`、远端目录缺失或
  本地目标已存在时都会在拉取前失败，避免拉到一半才失败；`--force` 只放宽“运行中”和
  “覆盖已有结果”（保留 `.bak`），远端目录缺失仍拒绝。
- 聚合指标输出 `median / p95 / min / max / n`。p95 用 **nearest-rank**，小样本不插值；
  `metrics` 字段用中位数保持旧消费方兼容，`samples` 保留分布。缺失值不补 0。
- 阶段状态按样本聚合：全部 `ok` → `ok`，全部失败/缺失 → `failed`，其余 → `partial`。
- 样本数少于 `--repeat`、某个 run 失败或阶段不完整，都会在报告和 `to-result.py`
  的 stderr 中显式提示，不会把 2 个样本写成 3 个。
- 总耗时约等于单次耗时 × N；这是为了真实可复现，不做并发加速。

### 「人话报告」长什么样

`report` 把数字翻译成结论，专为不熟悉指标的人设计；未测项会明确写「未测/不能判断」，
不会把缺数据猜成「流畅」或「存在争抢」：

```
【会不会卡？】← 轻量云最该看的一项
  磁盘 4K 随机读          4,962 IOPS      ⚠️ 偏弱
  磁盘长尾 p99           952.11 ms        ⚠️ 偏弱
  → ⚠️ 长尾严重：约 1% 的请求要等几百毫秒，建站会偶发卡顿

【CPU 够用吗？】
  单核性能                  445 events/s     一般
  物理核                      1 核（2 线程）  ⚠️ 超线程
  → 标称 2 核，实际只有 1 个物理核

【内存与网络】
  内存                        2 GB（标称）
  → ⚠️ 实测可用仅 1.6G，比标称少 20%（内核/虚拟化占用）

【这台能跑什么？】
  ✅ 个人博客 / 静态站 / 图床
  ⚠️ 数据库：能跑，但磁盘长尾会带来偶发卡顿
  ❌ 编译构建 / 视频转码 / 高并发站点
```

多次采样时，报告会显示类似：

```text
批次：20261106T000000Z-abc123  3/3 个样本  状态=ok
磁盘 4K 随机读  10,000（p95 12,000，范围 8,000~12,500，n=3） IOPS
⚠️ 若只有 2/3 个样本，报告会明确写出样本不足，而不是补零
```

> ⚠️ 跑生产机前请先读方法论里的「可比性约束」——业务负载、测试时段都会显著影响成绩。

## 八个阶段

| 阶段 | 内容 | 档位 | 关键产出 |
|---|---|---|---|
| `env` | 系统信息、物理核/超线程、指令集、虚拟化类型 | 全部 | 后续所有对比的前提 |
| `deps` | 按阶段安装 fio / sysbench / iperf3 / stress-ng / PTS | 全部 | quick 不装 PTS |
| `fio` | 磁盘精测：4K 随机 + 顺序读写 + **p99 长尾** | 全部 | 决定"卡不卡" |
| `sysbench` | CPU 单核/全核 + 内存带宽 | 全部 | 决定"强不强" |
| `stress` | 满载 + mpstat 记 %steal（5 / 10 / 20 分钟随档位） | 全部 | 决定"稳不稳" |
| `net` | Speedtest + 跨机 iperf3 | 全部 | 带宽真实值 |
| `yabs` | YABS 快速跑分 | standard+ | 与精测**交叉验证** |
| `pts` | Phoronix（standard=7-Zip+redis；full=全部 5 项含内核编译） | standard+ | 应用层性能 |

每个阶段的完整命令与参数、以及**读数据时的 6 项可比性约束**，见
**[docs/methodology.md](docs/methodology.md)**。

## 踩坑记录

**[docs/pitfalls.md](docs/pitfalls.md)** —— 15 条实测踩到的坑，每条按「现象 → 真因 → 解法」写。
其中几条代价很高（apt 卡死 45 分钟、PTS 日志写满 32GB、重跑覆盖导致数据丢失），
自己写评测脚本的话建议先扫一眼。

## 你的数据留在你自己的机器上

脚本**不会**自动上传任何东西。`results/` 已在 `.gitignore` 中排除——你的实测结果、
机器 IP、主机名全部留在本地。

想加入公开对比数据的话，跑完后提交到 Cloud Bench 站点（**站点尚未上线**，届时会
在此补充提交方式）。在此之前，结果可以完全自用。

## 仓库边界

| 内容 | 位置 |
|---|---|
| 评测脚本、方法论、踩坑记录 | **本仓库（公开）** |
| 数据集、对比站点服务端、部署配置 | 私有仓库（运营方维护） |

分界只有一条：**能给别人拿去复现测试的 → 公开；涉及你自己的数据或运维配置的 → 私有。**

## 交接与开发

要接手这个项目（或自己隔几周回来继续），先读 **[HANDOFF.md](HANDOFF.md)**——
仓库地图、**关键设计决策及其理由**、待办与未决问题、环境依赖都在那里。

> 那里记录「为什么是这样」，比读代码更快进入状态，也能避免把试出来的设计又改回去。

## 开发与验证

```bash
bash -n scripts/cloud-benchmark.sh
shellcheck -S warning scripts/cloud-benchmark.sh
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -v
git diff --check
```

当前 29 个离线测试：
- `tests/test_bench_metrics.py`：fio p99/p99.9、AVX-512、阶段状态、Speedtest 回退、
  批次中位数/p95/缺失样本/部分 run 失败。
- `tests/test_shell_batch.py`：mock `run --repeat 2` → `status/collect/report --batch`
  端到端链路、`repeat=1` 兼容、运行中批次拒绝回收。
- `tests/test_shell_collect.py` / `test_shell_pts.py`：单 run 状态机与 PTS partial。

这些是**离线 mock 验证**；真实主机、真实 PTS、私有服务端对新增 `batch/samples`
字段的兼容性仍未验收。schema_version 保持 2，旧消费方可继续只读 `metrics`。

## 许可

待定。
