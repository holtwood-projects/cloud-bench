#!/usr/bin/env bash
# =============================================================================
# 多云轻量主机性能横评 —— 统一评测脚本（可复用）
# =============================================================================
# 目标：对多家云商的轻量型云主机跑同一套固定流程，产出可横向对比的数据。
#
# 用法：
#   0) 追加新主机：编辑 results/hosts.tsv 加一行，然后按 1) 跑即可
#   1) 远端跑测试（tmux 后台执行，SSH 断连不中断）
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01
#         默认档位 quick（约 16 分钟）：磁盘 p99 / 超售 %steal / 物理核识别
#         —— 轻量云最关心的三项全在这里，且是 YABS/bench.sh 测不出来的
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --profile standard
#         约 40 分钟：加 YABS 交叉验证 + PTS（7-Zip / redis）
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --profile full
#         约 2.5 小时：全量，含 PTS 内核编译（轻量云一般用不到）
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --stages env,deps,fio
#         只跑指定阶段（逗号分隔，逐项覆盖档位预设）
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --at 23:30   # 远端定时开跑
#         （--at 的等待在**远端**计算，本机关机/断网都不影响；跑生产机时用它卡低峰窗口）
#   2) 多次采样（N>=2 时远端顺序执行 N 个独立 run，写批次清单）
#      ./scripts/cloud-benchmark.sh run --name mycloud-2c2g-01 --repeat 3
#   3) 查看进度（tail 远端日志 / 阶段完成情况）
#      ./scripts/cloud-benchmark.sh status --name tencent-2c2g-03
#      ./scripts/cloud-benchmark.sh status --name tencent-2c2g-03 --batch <batch_id>
#   4) 回收结果到 results/<name>/<run_id>/
#      ./scripts/cloud-benchmark.sh collect --name tencent-2c2g-03
#      ./scripts/cloud-benchmark.sh collect --name tencent-2c2g-03 --batch <batch_id>
#      每次 run 有独立 run_id；status/collect/report/submit 可用 --run <ID> 指定旧运行
#   5) 跨主机 iperf3 互测（双向 -P 8，需要两台的 ssh 别名都可直连）
#      ./scripts/cloud-benchmark.sh iperf --a tencent-2c2g-03 --b volc-4c8g-01
#   6) 汇总所有已回收结果 → results/summary/compare.{csv,md}
#      ./scripts/cloud-benchmark.sh summarize
#      ./scripts/cloud-benchmark.sh summarize --batch <batch_id>
#   7) 单机「人话报告」——面向入门：把数字翻译成「会不会卡 / 是不是虚标 / 能跑什么」
#      ./scripts/cloud-benchmark.sh report --name mycloud-2c2g-01
#      ./scripts/cloud-benchmark.sh report --name mycloud-2c2g-01 --batch <batch_id>
#   8) 提交结果到站点（先 --dry-run 看一眼要传什么，确认后再提交）
#      ./scripts/cloud-benchmark.sh submit --name mycloud-2c2g-01 --api https://bench.example.com --dry-run
#      export BENCH_API_KEY=xxx     # 密钥用环境变量传，别写进命令行（会留在 shell 历史）
#      ./scripts/cloud-benchmark.sh submit --name mycloud-2c2g-01 --api https://bench.example.com
#      ./scripts/cloud-benchmark.sh submit --name mycloud-2c2g-01 --batch <batch_id> --api https://bench.example.com
#
# 设计原则：
#   - 被测主机上不安装任何 AI agent / 常驻进程，只装评测工具（fio/sysbench/...），
#     保持被测环境干净（agent 进程会占用 CPU/内存，直接污染跑分）
#   - 所有长任务在远端 tmux（无 tmux 时 setsid+nohup）中执行，SSH 断连不中断
#   - 每阶段独立日志 + 失败不阻塞后续阶段（记录到 failed-stages.txt）
#   - 关键指标写入 metrics.tsv；汇总优先重解析原始产物，metrics.tsv 只作旧版兜底
#   - 幂等：同一阶段重复跑会覆盖该阶段产物，不产生重复 metric（先删旧的同名 metric）
#
# 阶段说明（--stages 可任选，逗号分隔；all = 全部）：
#   env      前置检查 + 系统信息（lscpu/free/lsblk/os-release/虚拟化）→ system-info.txt
#   deps     安装评测工具（fio sysbench iperf3 stress-ng sysstat + PTS 的 php 扩展）
#   yabs     YABS 快速跑分（CPU + fio + iperf3 一次出结果，保留 JSON）
#   pts      Phoronix Test Suite（**standard/full 档才跑**；内核编译一项就要 87 分钟，
#            轻量云场景用不到，故 quick 档默认跳过）
#   fio      磁盘精测：4K 随机读/写（direct=1,bs=4k,iodepth=32,numjobs=4,60s）+ 顺序读写
#   sysbench CPU 单线程/全核各 60s + 内存带宽
#   stress   超售检测：stress-ng --cpu $(nproc) 满载 + mpstat 1 记录 %steal
#            （时长随档位：quick 5m / standard 10m / full 20m，--stress-mins 可覆盖）
#   net      公网 Speedtest（跨机 iperf3 用 iperf 子命令单独跑）
#
# 注意：
#   - 被测主机在跑业务时，deps/yabs/fio/sysbench/stress 都会抢占资源，必须低峰期执行；
#     业务高峰只适合跑 env/net 这类轻量阶段。
#   - 火山云磁盘余量小（<15G）时，fio 自动把测试文件降到 1G×4，测完立即删除。
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
SCRIPT_VERSION="1.2.0"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 产物目录：默认本仓库的 results/，可用 CB_RESULTS_DIR 覆盖。
# 这样「工具在公开仓库、实测数据在私有工作区」也能跑——评测数据是使用者自己的，
# 没必要（也不应该）塞进工具仓库。
RESULTS_DIR="${CB_RESULTS_DIR:-$REPO_ROOT/results}"
HOSTS_FILE="$RESULTS_DIR/hosts.tsv"
# 远端工作目录：用 ~ 让远端 shell 展开，root 与普通用户（ubuntu）都适用。
# 注意：这两个变量传给远端时**不能加引号**，否则 ~ 不会展开。
# shellcheck disable=SC2088  # 故意保留 ~，由远端 shell 展开
REMOTE_BASE='~/cloud-benchmark'
REMOTE_RUNS="$REMOTE_BASE/runs"
REMOTE_BATCHES="$REMOTE_BASE/batches"
REMOTE_LATEST="$REMOTE_BASE/latest-run"
REMOTE_LATEST_BATCH="$REMOTE_BASE/latest-batch"
REMOTE_OUT="$REMOTE_BASE/out"   # 旧版扁平目录，仅用于兼容
TMUX_SESSION="cloudbench"

# ⚠️ 必须给本脚本单独指定 ControlPath。用户 ~/.ssh/config 里配了
#    `ControlMaster auto` + `ControlPath ~/.ssh/cm-%C` + `ControlPersist 10m`，
#    新连接会复用**已存在（可能已僵死）的主连接**——被测机满负载跑 PTS 时表现为
#    「SSH 全部卡住、命令不返回」，实测加 -o ControlPath=none 后立即秒回，
#    排查时一度误判为 CPU 饱和导致 sshd 饿死。
#    这里用独立 socket 路径：既不被其他会话的僵死连接拖累，又保留脚本内的连接复用。
SSH_OPTS=(-o ConnectTimeout=15 -o ServerAliveInterval=60 -o ServerAliveCountMax=3 \
          -o StrictHostKeyChecking=accept-new \
          -o ControlMaster=auto -o ControlPath="$HOME/.ssh/cb-%r@%h:%p" -o ControlPersist=5m)

# 各阶段超时（秒）—— 轻量主机 + 1M 带宽下实测：YABS 单轮可达 2 小时
TIMEOUT_YABS=14400      # 4h
TIMEOUT_PTS=32400       # 9h（build-linux-kernel 在 1核2G 上极慢）
TIMEOUT_FIO=1800        # 30m
TIMEOUT_SYSBENCH=900    # 15m
TIMEOUT_STRESS=1800     # 30m（压测本身 20m）
TIMEOUT_NET=900         # 15m

ALL_STAGES="env deps yabs pts fio sysbench stress net"

# ---------------------------------------------------------------------------
# 评测档位（profile）—— 为「轻量云 + 初级用户」场景预设
# ---------------------------------------------------------------------------
# 设计依据：实测耗时（腾讯云4C4G 空机，无业务干扰）
#   pts   1:49:20  ← 占 74%，其中 build-linux-kernel 一项 87 分钟（占全脚本 59%）
#   stress   20:03 · yabs 7:40 · fio 5:05 · deps 3:08 · sysbench 2:10 · env+net <1min
#   → 全量约 2 小时 28 分
#
# 为什么默认砍 PTS：轻量云用户不会编译内核，也不关心 openssl/ramspeed 跑分；
# 而**全部差异化指标都在这三项之外**——
#   磁盘 p99 长尾 → fio  ·  超售 %steal → stress  ·  物理核/超线程 → env
# 砍掉通用跑分后，quick 档 16 分钟就能拿到 YABS/bench.sh 测不出的数据。
#
# profile 只提供默认值；显式 --stages / --pts-tests / --stress-mins 可逐项覆盖。
PROFILE_QUICK="env deps fio sysbench stress net"
PROFILE_STANDARD="env deps yabs fio sysbench stress net pts"
PTS_QUICK=""                          # quick 不跑 PTS
PTS_STANDARD="compress-7zip redis"    # 只留最直观、最快的两项
PTS_FULL="compress-7zip openssl build-linux-kernel ramspeed redis"
STRESS_QUICK=5                        # 5 分钟足够暴露持续超售
STRESS_STANDARD=10
STRESS_FULL=20                        # 与历史数据口径一致

# ---------------------------------------------------------------------------
# 日志与输出
# ---------------------------------------------------------------------------
c_info()  { printf '\033[36m[INFO]\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[31m[ERR ]\033[0m %s\n' "$*" >&2; }
c_head()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

die() { c_err "$*"; exit 1; }

usage() {
  echo "cloud-benchmark.sh  v$SCRIPT_VERSION"
  echo
  sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---------------------------------------------------------------------------
# 主机清单解析
# ---------------------------------------------------------------------------
# hosts.tsv 列：name ssh_alias vendor spec vcpu ram_gb bandwidth price_cny price_note role
host_field() {
  local name="$1" col="$2"
  [ -f "$HOSTS_FILE" ] || die "主机清单不存在：$HOSTS_FILE"
  awk -F'\t' -v n="$name" -v c="$col" '
    /^[[:space:]]*#/ || NF < 2 { next }
    $1 == n { print $c; found=1; exit }
    END { if (!found) exit 1 }
  ' "$HOSTS_FILE"
}

host_target() {
  local name="$1" alias
  alias="$(host_field "$name" 2)" || die "主机清单里没有 '$name'（见 $HOSTS_FILE）"
  echo "$alias"
}

# 最新一次运行的 ID（远端 latest-run 文件；旧版目录返回空）
latest_run_id() {
  local target="$1"
  rsh "$target" "cat $REMOTE_LATEST 2>/dev/null" 2>/dev/null | head -n1 | tr -d '\r' || true
}

# --run 会进入远端 shell 命令与本地结果路径；只允许安全字符，避免路径穿越/命令注入。
valid_run_id() {
  printf '%s\n' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'
}

require_run_id() {
  [ -z "$1" ] || valid_run_id "$1" || \
    die "非法运行 ID: $1（只允许字母、数字、点、下划线、短横线）"
}

# --batch 同样会进入远端路径与本地清单文件；规则与 run_id 一致。
valid_batch_id() {
  printf '%s\n' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'
}

require_batch_id() {
  [ -z "$1" ] || valid_batch_id "$1" || \
    die "非法批次 ID: $1（只允许字母、数字、点、下划线、短横线）"
}

# 远端批次清单是 key<TAB>value；这里只在本地解析已经拉回来的文本。
batch_field() {
  local manifest="$1" key="$2"
  printf '%s\n' "$manifest" | awk -F'\t' -v k="$key" '$1 == k { print $2; exit }'
}

# ---------------------------------------------------------------------------
# 远端执行封装
# ---------------------------------------------------------------------------
rsh() {  # rsh <target> <command...>
  local target="$1"; shift
  ssh "${SSH_OPTS[@]}" "$target" "$@"
}

# 远端下载封装：若本机 127.0.0.1:8894 有 mihomo 在监听，则 curl 自动走代理。
# ⚠️ 实测踩坑（2026-09-13，腾讯云 04）：无代理机器直连海外源极慢/失败——
#    speedtest CLI（install.speedtest.net，Cloudflare 前置）与 yabs.sh（GitHub）
#    国内直连不可靠；而有 mihomo 的机器（01/02/03）下载飞快。
#    PTS 下载已由 pts_ensure_batch_config 的 --pts-proxy 覆盖，这里管 CLI 下载。
cb_curl() {  # cb_curl <url> -o <file>
  local args=("$@")
  if ss -tln 2>/dev/null | grep -q ':8894 '; then
    curl -sL -x http://127.0.0.1:8894 "${args[@]}"
  else
    curl -sL "${args[@]}"
  fi
}

# ---------------------------------------------------------------------------
# 远端执行器（exec 模式）—— 下面所有 stage_* 都在被测主机上运行
# ---------------------------------------------------------------------------
need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo -n"; fi
}

# 某阶段重跑成功后，同时清掉 failed / partial 的历史记录（幂等）。
# ⚠️ 这里必须分两行声明 local：bash 的 `local a="$1" b="$a"` 中，同一行内的 $a
#    取到的是**外部同名变量**（实测 `local out="AAA" g="$out-BBB"` → g 为 "-BBB"，
#    即 $out 求值为空）。写成一行会让 f 变成 "-failed-stages.txt" 这种错误路径。
clear_failed() {
  local out="$1" stage="$2"
  local f
  for f in "$out/failed-stages.txt" "$out/partial-stages.txt"; do
    [ -f "$f" ] || continue
    awk -v s="$stage" '$0 != s' "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
    [ -s "$f" ] || rm -f "$f"
  done
}

# 写一次运行的终态；stage-status.tsv 记录每个阶段的 ok/partial/failed。
write_run_status() {
  local out="$1" status="$2" started="$3" finished="$4" stages="$5"
  {
    printf 'run_id\t%s\n' "${CB_RUN_ID:-}"
    printf 'profile\t%s\n' "${CB_PROFILE:-}"
    printf 'status\t%s\n' "$status"
    printf 'started_at\t%s\n' "$started"
    printf 'finished_at\t%s\n' "$finished"
    printf 'stages_requested\t%s\n' "$stages"
  } > "$out/run-status.tsv"
}

append_stage_status() {
  local out="$1" stage="$2" status="$3" started="$4" finished="$5"
  printf '%s\t%s\t%s\t%s\n' "$stage" "$status" "$started" "$finished" \
    >> "$out/stage-status.tsv"
}

# 向 metrics.tsv 追加一条指标（同 metric 名先删旧行，保证幂等）
metric() {
  local out="$1" key="$2" val="$3" unit="${4:-}"
  [ -n "$val" ] && [ "$val" != "null" ] || return 0
  local f="$out/metrics.tsv"
  touch "$f"
  awk -F'\t' -v k="$key" '$1 != k' "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
  printf '%s\t%s\t%s\n' "$key" "$val" "$unit" >> "$f"
}

# 读取 metrics.tsv 中某个键的值；不存在时输出空。
metric_value() {
  local out="$1" key="$2"
  [ -f "$out/metrics.tsv" ] || return 0
  awk -F'\t' -v k="$key" '$1 == k { print $2; exit }' "$out/metrics.tsv"
}

stage_env() {
  local out="$1"
  local f="$out/system-info.txt"
  need_sudo
  {
    echo "########## 采集时间 ##########"
    date -Is
    echo "hostname: $(hostname)"
    echo
    echo "########## 前置检查 ##########"
    echo "--- 磁盘剩余 ---"
    df -h / /boot 2>/dev/null
    echo "--- 当前负载 ---"
    echo "loadavg: $(cat /proc/loadavg 2>/dev/null || echo n/a)"
    uptime
    echo "--- CPU 占用 TOP5 ---"
    ps -eo pcpu,pmem,comm --sort=-pcpu | head -6
    echo "--- 内存占用 TOP5 ---"
    ps -eo pcpu,pmem,comm --sort=-pmem | head -6
    echo "--- 在跑的业务进程（非系统）---"
    ps -eo comm --no-headers | sort | uniq -c | sort -rn | head -15
    echo "--- 容器（如有）---"
    $SUDO docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null || echo "（无 docker 或无权限）"
    echo
    echo "########## CPU ##########"
    lscpu
    echo "--- 在线 CPU / 物理核 / 线程 ---"
    echo "nproc=$(nproc)"
    lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l | sed 's/^/物理核数(去重 Core,Socket)=/'
    # CPU 指令集单行摘要：lscpu 的 Flags 是 80 列折行的（单词会被截断成 "pg"+"e"），
    # 直接 grep 会漏判。评测里这是硬指标（天翼云屏蔽 AVX 直接导致 stress-ng SIGILL）。
    echo "--- CPU 指令集（单行不折行）---"
    grep -m1 '^flags' /proc/cpuinfo | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//'
    echo
    echo "--- 关键指令集判定 ---"
    for feat in avx avx2 avx512f fma aes sse4_2 rdrand; do
      printf '%-10s ' "$feat"
      grep -m1 '^flags' /proc/cpuinfo | grep -qw "$feat" && echo '有' || echo '无'
    done
    echo
    echo "########## 内存 ##########"
    free -h
    echo "--- swap ---"
    swapon --show 2>/dev/null || echo "无 swap"
    echo
    echo "########## 磁盘 ##########"
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL
    echo "--- 调度器 ---"
    for d in /sys/block/vd*/queue/scheduler /sys/block/sd*/queue/scheduler /sys/block/nvme*/queue/scheduler; do
      [ -r "$d" ] && echo "$d: $(cat "$d")"
    done
    echo
    echo "########## 操作系统 ##########"
    cat /etc/os-release
    echo "kernel: $(uname -r)"
    echo "虚拟化类型: $(systemd-detect-virt 2>/dev/null || echo unknown)"
    echo "云厂商 DMI: $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo n/a) / $(cat /sys/class/dmi/id/product_name 2>/dev/null || echo n/a)"
    echo
    echo "########## 频率与调度 ##########"
    echo "cpufreq governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a)"
    echo "当前 MHz(各核): $(grep -h 'cpu MHz' /proc/cpuinfo 2>/dev/null | awk -F': ' '{printf "%s ", $2}')"
  } > "$f" 2>&1
  c_ok "system-info.txt 已生成"
}

stage_deps() {
  local out="$1"
  need_sudo
  local log="$out/deps.log"
  # 依赖按阶段裁剪：quick 不装 PTS / 内核编译依赖，避免默认档被网络和 apt 拖慢。
  local need_pts="${CB_NEED_PTS:-1}"
  local need_kernel="${CB_NEED_KERNEL_DEPS:-$need_pts}"
  {
    echo "=== 安装评测工具 $(date -Is) ==="
    # ⚠️ 四个实测踩坑（2026-09-11，两台生产机上分别踩到，全部已复现确认）：
    #   1) DEBIAN_FRONTEND 必须**显式经 sudo 传递**（用 `env` 前缀）：
    #      sudo 默认 env_reset 会把 `export DEBIAN_FRONTEND` 清掉，导致 debconf 在
    #      tmux 的 pty 下拉起 whiptail 交互框，卡在
    #      "Start Iperf3 as a daemon automatically?" 永不返回（实测卡 157s+ 到 45min）。
    #   2) 只重定向 stdin 到 /dev/null **无效**：whiptail 直接打开 /dev/tty，不看 stdin。
    #   3) 用 debconf-set-selections 预设答案兜底（即使前面都失效也不会卡）。
    #   4) apt 加超时 + 优先直连绕过业务代理：生产机常配全局 mihomo 代理
    #      （Acquire::http::Proxy "http://127.0.0.1:8894"）和国外源
    #      （deb.nodesource.com / cli.github.com / security.ubuntu.com），
    #      代理一挂 apt 就整体卡死（腾讯云实测 209s 零下载）。国内镜像直连更快，
    #      且不依赖业务进程存活；直连装不齐时再回退系统代理配置重试。
    local APT_ENV=(env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true)
    local APT_OPTS=(-o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=1)
    local NOPROXY=(-o Acquire::http::Proxy=false -o Acquire::https::Proxy=false)

    # 快速路径：工具齐全就不再动 apt（幂等，重复跑很快）
    # ⚠️ 检查清单必须含 make/gcc/cc：PTS 测试安装时都要编译，
    #    漏了它们会导致「工具全齐、PTS 却全失败」（实测踩坑 2026-09-13：
    #    腾讯云 01 的 build-essential 没装上，deps 快速路径却判「齐全」跳过，
    #    PTS 4 个测试全部 make/cc not found）。
    local req=(fio sysbench iperf3 stress-ng mpstat jq curl)
    if [ "$need_pts" -eq 1 ]; then
      req+=(php make gcc git bc)
    fi
    if [ "$need_kernel" -eq 1 ]; then
      req+=(flex bison bc)
    fi
    local missing=""
    for c in "${req[@]}"; do
      command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
    done
    if [ -z "$missing" ]; then
      echo "评测工具已齐全，跳过 apt 安装"
    else
      echo "缺失工具:$missing"
      local PKGS=(fio sysbench iperf3 stress-ng sysstat curl jq bc)
      if [ "$need_pts" -eq 1 ]; then
        PKGS+=(php-cli php-xml php-gd php-bz2 build-essential git bc)
      fi
      if [ "$need_kernel" -eq 1 ]; then
        PKGS+=(flex bison bc libelf-dev libssl-dev)
      fi
      # ⚠️ build-essential 必须装（实测踩坑 2026-09-13）：PTS 的测试安装时都要编译
      #    （7-Zip / openssl / ramspeed / redis / build-linux-kernel），cloud image
      #    （腾讯云 01）通常不带 gcc/make——实测全部 `make: command not found` /
      #    `cc: command not found`，PTS 4 个测试安装全部失败、一个成绩都没有。
      #    天翼云的 Ubuntu Server 镜像自带 gcc/make 所以此前没暴露这个问题。
      # ⚠️ flex/bison 也必须装（实测踩坑 2026-09-13，真正根因）：build-linux-kernel
      #    编译时执行 `flex: not found` 直接失败（3 次 run 全部 quit），PTS 日志只留
      #    `failed to properly run` 无具体错误，一度误判为内存不足——手动跑才看到
      #    `E: /bin/sh: 1: flex: not found`。Linux 内核构建需要 flex+bison，
      #    cloud image 不带，而天翼云 Ubuntu Server 自带故此前未暴露。
      # ⚠️ libelf-dev/libssl-dev/bc 也必须装（同轮踩坑续）：补完 flex/bison 后
      #    编译报 `fatal error: gelf.h: No such file`（objtool 需要 libelf-dev），
      #    后续可能还需 openssl 头与 bc。一次装齐内核构建完整依赖链。
      # 兜底：预设 iperf3 的 debconf 答案（即使前端仍被拉起也不会阻塞）
      printf 'iperf3 iperf3/daemon boolean false\n' | \
        $SUDO "${APT_ENV[@]}" debconf-set-selections 2>/dev/null || true

      # 第一轮：直连（绕过业务代理）
      $SUDO "${APT_ENV[@]}" apt-get update -qq "${APT_OPTS[@]}" "${NOPROXY[@]}" < /dev/null 2>&1 | tail -3
      $SUDO "${APT_ENV[@]}" apt-get install -y -qq "${APT_OPTS[@]}" "${NOPROXY[@]}" "${PKGS[@]}" < /dev/null 2>&1 | tail -20

      # 第二轮：直连装不齐时，回退系统代理配置重试
      local still=""
      for c in "${req[@]}"; do
        command -v "$c" >/dev/null 2>&1 || still="$still $c"
      done
      if [ -n "$still" ]; then
        echo "直连后仍缺:$still —— 回退系统 apt 代理配置重试"
        $SUDO "${APT_ENV[@]}" apt-get update -qq "${APT_OPTS[@]}" < /dev/null 2>&1 | tail -3
        $SUDO "${APT_ENV[@]}" apt-get install -y -qq "${APT_OPTS[@]}" "${PKGS[@]}" < /dev/null 2>&1 | tail -20
      fi
    fi
    echo
    echo "=== 版本确认 ==="
    # 存在性用 command -v 判断；版本号尽力而为（mpstat 等不支持 --version，不能据此判缺失）
    local ver_cmds=(fio sysbench iperf3 stress-ng mpstat jq)
    [ "$need_pts" -eq 1 ] && ver_cmds+=(php)
    for c in "${ver_cmds[@]}"; do
      if command -v "$c" >/dev/null 2>&1; then
        printf '%-14s OK   %s\n' "$c" "$("$c" --version 2>&1 | head -1)"
      else
        printf '%-14s 缺失!\n' "$c"
      fi
    done
    echo
    echo "--- PTS ---"
    if [ "$need_pts" -eq 0 ]; then
      echo "本次阶段未包含 pts，跳过 PTS 安装"
    elif command -v phoronix-test-suite >/dev/null 2>&1; then
      phoronix-test-suite --version 2>&1 | head -3
    else
      echo "PTS 未安装，尝试安装到 /tmp（与历史测试口径一致）"
      cd /tmp && git clone --depth 1 https://github.com/phoronix-test-suite/phoronix-test-suite.git 2>&1 | tail -2
      if [ -f /tmp/phoronix-test-suite/phoronix-test-suite ]; then
        # ⚠️ 源码方式必须用官方 **install-sh** 安装（实测踩坑 2026-09-13，折腾多轮）：
        #    源码目录里的 phoronix-test-suite 只是个**存根**（73 字节），直接运行会报
        #    「you must first change directories to phoronix-test-suite or install the
        #      program using the install-sh script」并立刻退出（141/SIGPIPE），
        #    表现为「阶段完成但零成绩」。两个看似可行的错误做法：
        #      · 建符号链接 → PTS 按自身路径推断安装位置，链接后找不到自己；
        #      · 自建 wrapper  `cd /tmp/phoronix-test-suite && exec ./phoronix-test-suite "$@"`
        #        → **自我递归**（cd 到自己目录再 exec 自己），同样跑不起来。
        #    唯一可靠做法：跑 install-sh（装出 /usr/bin/phoronix-test-suite，
        #    存根会从 73 字节变为正常启动脚本）。
        cd /tmp/phoronix-test-suite && $SUDO ./install-sh >/dev/null 2>&1
        # 清掉可能残留的同名文件：/usr/local/bin 的 PATH 优先级高于 /usr/bin
        $SUDO rm -f /usr/local/bin/phoronix-test-suite
      fi
      if command -v phoronix-test-suite >/dev/null 2>&1; then
        phoronix-test-suite --version 2>&1 | head -3
      else
        echo "PTS 缺失!"
      fi
    fi
  } > "$log" 2>&1
  if grep -q '缺失!' "$log"; then
    c_warn "部分工具安装失败，见 $log"
    echo "deps" >> "$out/failed-stages.txt"
  else
    clear_failed "$out" deps
    c_ok "评测工具就绪"
  fi
}

stage_yabs() {
  local out="$1"
  local log="$out/yabs.log"
  rm -f "$out/yabs-result.json" "$out/yabs_result.json" "$out/result.json"
  {
    echo "=== YABS $(date -Is) ==="
    cd "$out" || exit 1
    [ -f yabs.sh ] || cb_curl https://yabs.sh -o yabs.sh
    # -j 输出 JSON；-w 写 JSON 文件；保留完整日志
    # ⚠️ 加 **-g 跳过 Geekbench**（实测踩坑 2026-09-13）：
    #    YABS 会从**海外 CDN**（cdn.geekbench.com → Linode）下载 100MB+ 的
    #    Geekbench 6 包，国内极慢（实测两台腾讯云各卡在 curl 下载 18 分钟以上，
    #    整个 YABS 被拖住）。而 YABS v2026-07-24 与 Geekbench 6 存在兼容问题，
    #    它解析出的 single/multi 分数**恒为 null**（用户 2026-09-10 的手动测试
    #    同样如此，只有结果 URL）——即这个下载**换不来任何可用数据**，
    #    纯属浪费时间与带宽。故直接跳过；若将来 YABS 修好了解析，
    #    再去掉此参数并承担下载耗时。
    # 注：源方案写的是 `curl -sL yabs.sh | bash`，这里先下载再执行以便传参和留痕，等价
    timeout "$TIMEOUT_YABS" bash yabs.sh -j -w yabs-result.json -g
    echo "=== YABS 退出码: $? $(date -Is) ==="
  } > "$log" 2>&1

  # 从 JSON 提取关键指标
  # YABS 的 -w 指定文件名；不同版本也可能落成 yabs_result.json，做一次兼容
  local jj=""
  for cand in "$out/yabs-result.json" "$out/yabs_result.json" "$out/result.json"; do
    [ -s "$cand" ] && { jj="$cand"; break; }
  done
  if [ -n "$jj" ] && jq -e '.fio and (.fio | length > 0)' "$jj" >/dev/null 2>&1; then
    # YABS v2026-07-24 的真实 JSON 结构（首轮按 .read.iops 解析全部落空，实测校正）：
    #   fio[]   : {bs, iops_r, iops_w, iops_rw, speed_r, speed_w, speed_rw, speed_units}
    #   cpu     : 只有元数据（model/cores/freq/aes/virt），**没有跑分**
    #   geekbench[]: {version, single, multi, url}（single/multi 常为 null，见下）
    metric "$out" "yabs_4k_read_iops"  "$(jq -r '.fio[] | select(.bs=="4k") | .iops_r // empty' "$jj" 2>/dev/null)" "IOPS"
    metric "$out" "yabs_4k_write_iops" "$(jq -r '.fio[] | select(.bs=="4k") | .iops_w // empty' "$jj" 2>/dev/null)" "IOPS"
    metric "$out" "yabs_4k_total_iops" "$(jq -r '.fio[] | select(.bs=="4k") | .iops_rw // empty' "$jj" 2>/dev/null)" "IOPS"
    metric "$out" "yabs_64k_total_iops" "$(jq -r '.fio[] | select(.bs=="64k") | .iops_rw // empty' "$jj" 2>/dev/null)" "IOPS"
    # ⚠️ YABS v2026-07-24 与 Geekbench 6 存在兼容问题：分数恒为 null，只有结果 URL
    #    （用户 2026-09-10 的手动测试同样如此），需从网页查看分数，非本次操作所致。
    metric "$out" "yabs_geekbench_url" "$(jq -r '.geekbench[0].url // empty' "$jj" 2>/dev/null)" ""
    metric "$out" "yabs_geekbench_single" "$(jq -r '.geekbench[0].single // empty' "$jj" 2>/dev/null)" "分"
    metric "$out" "yabs_geekbench_multi"  "$(jq -r '.geekbench[0].multi // empty' "$jj" 2>/dev/null)" "分"
    metric "$out" "yabs_elapsed_sec" "$(jq -r '.runtime.elapsed // empty' "$jj" 2>/dev/null)" "s"
    if jq -e '.fio[] | select(.bs=="4k") | (.iops_rw // .iops_r // .iops_w)' "$jj" >/dev/null 2>&1; then
      clear_failed "$out" yabs
      c_ok "YABS 完成（JSON: $(basename "$jj")）"
    else
      c_warn "YABS JSON 没有可用的 4K fio 结果，见 $log"
      echo "yabs" >> "$out/failed-stages.txt"
    fi
  else
    c_warn "YABS 未产出有效 JSON，见 $log"
    echo "yabs" >> "$out/failed-stages.txt"
  fi
}

# 确保 PTS 的 batch 模式已配置。
# ⚠️ 实测踩坑（2026-09-11 三台全中）：PTS 首次使用必须先配置 batch 模式，
#    否则 `batch-benchmark` 只把 6 个测试**下载安装完就退出**——日志末尾仅有一句
#    `[NOTICE] The batch mode must first be configured`，**退出码仍是 0**（伪装成功）。
#    表现为：日志里全是 Downloading/Installing，没有任何 Test/Average 结果。
#
# 用官方 `batch-setup` 配置（对两种安装方式都有效，自动写对路径）：
#   deb 包安装（天翼云）→ /var/lib/phoronix-test-suite/core.pt2so
#   源码安装（腾讯云/火山云）→ ~/.phoronix-test-suite/user-config.xml
# 不要用 sed 改 user-config.xml：deb 安装下该文件根本不存在（天翼云实测）。
#
# batch-setup 依次问 7 个问题，输入序列「y n n n n n y」对应：
#   y  Save test results when in batch mode         (Y/n) → 保存结果
#   n  Open the web browser automatically          (y/N) → 不自动开浏览器
#   n  Auto upload results to OpenBenchmarking.org (Y/n) → 不上传
#   n  Prompt for test identifier                  (Y/n) → 不提示
#   n  Prompt for test description                 (Y/n) → 不提示
#   n  Prompt for saved results file-name          (Y/n) → 不提示
#   n  Run all test options                        (Y/n) → **只跑默认选项**
# ⚠️ 两处都踩过坑：
#   ① 全部回车取默认值是错的——第 3~6 问默认 Y，会继续交互；
#   ② 第 7 问「Run all test options」绝不能答 y！答 y 会跑**每个测试的全部变体组合**，
#      fio 一个测试就有 1573 个组合，PTS 自报 `Estimated Time To Completion: 7 Days`
#      （天翼云实测），完全不可用。评测只要默认口径，必须答 n。
pts_ensure_batch_config() {
  echo "=== 配置 PTS batch 模式（batch-setup；输入序列 y n n n n n n）==="
  printf 'y\nn\nn\nn\nn\nn\nn\n' | phoronix-test-suite batch-setup 2>&1 | tail -3
  # 校验：未配置时 batch 前缀命令会打印 NOTICE
  if phoronix-test-suite batch-list-available-tests 2>&1 | grep -q "batch mode must first be configured"; then
    echo "[WARN] batch 模式仍未配置，PTS 可能只下载不运行"
    return 1
  fi

  # ⚠️ PTS 代理自动配置（实测踩坑 2026-09-13）：PTS 下载测试包走
  #    user-config.xml 的 Networking 段代理。无代理的机器直连国外源极慢
  #    （腾讯云 04 实测下载 linux-7.0.tar.xz 要 46 分钟），配了代理后 1 分钟。
  #    调度端可设 CB_PTS_PROXY="127.0.0.1:8894"（cmd_run --pts-proxy），
  #    脚本自动写入 PTS 配置；不设则保持现状（机器自带代理配置则不受影响）。
  local ucfg="$HOME/.phoronix-test-suite/user-config.xml"
  if [ -n "${CB_PTS_PROXY:-}" ] && [ -f "$ucfg" ]; then
    local paddr="${CB_PTS_PROXY%%:*}" pport="${CB_PTS_PROXY##*:}"
    sed -i \
      -e "s|<ProxyAddress>[^<]*</ProxyAddress>|<ProxyAddress>$paddr</ProxyAddress>|" \
      -e "s|<ProxyPort>[^<]*</ProxyPort>|<ProxyPort>$pport</ProxyPort>|" \
      "$ucfg"
    echo "PTS 代理已配置: $CB_PTS_PROXY"
  fi
  echo "PTS batch 模式已配置并校验通过"
}

stage_pts() {
  local out="$1"
  local log="$out/pts.log"
  need_sudo
  {
    echo "=== Phoronix Test Suite $(date -Is) ==="
    cd "$out" || exit 1
    command -v phoronix-test-suite >/dev/null 2>&1 || { echo "PTS 未安装"; return 1; }
    pts_ensure_batch_config || { echo "PTS batch 模式配置失败"; echo "pts" >> "$out/failed-stages.txt"; return 1; }
    export PTS_CONCURRENT_TEST_RUNS=1
    export PTS_SILENT_MODE=1          # 无人值守：不打印进度动画、不问交互问题
    export PTS_BATCH_MODE=1
    # 禁用彩色输出：PTS 检测到 pty（有 tmux 的机器）会输出 ANSI 颜色码，
    # 形如 `Average: \x1b[1;34m16484 MIPS\x1b[0m`，会让下游的
    # `Average:\s*[0-9.]+` 解析全部失配（实测：火山云有 tmux 因而带色、
    # 天翼云用 nohup 无色，同样脚本结果却不同）。TERM=dumb 可关闭彩色。
    export TERM=dumb

    # ⚠️ 日志看门狗（严重事故防护，实测踩坑 2026-09-12）：
    #    PTS 某些失败路径会疯狂刷日志——pts/fio 的默认引擎 IO_uring 在云 VM 上直接
    #    失败，PTS 一边重试一边输出错误，实测把 pts.log 写到 **29~37GB**、以约
    #    11MB/s 持续增长，三台磁盘全部被写到 100% 占满（含两台跑着生产业务的机器）。
    #    这里每 30s 检查一次，超过 500MB 就**截断日志 + 杀掉 PTS**：
    #    ⚠️ 只截断不够（2026-09-13 复查）——PTS 进程持有旧文件描述符，
    #    截断后它继续写**已删除的 inode**，磁盘空间被「幽灵文件」持续占用，
    #    直到 PTS 退出才释放。日志涨到 500MB 说明 PTS 已死循环，直接杀更干净。
    local logfile="$out/pts.log"
    (
      while sleep 30; do
        [ -f "$logfile" ] || continue
        # 注意：这里是 ( ) 子 shell，不能用 local（local 只在函数上下文有效）
        sz=$(stat -c%s "$logfile" 2>/dev/null || echo 0)
        if [ "$sz" -gt 524288000 ]; then
          printf '\n[WATCHDOG] pts.log 达 %s 字节（异常），终止 PTS\n' "$sz" >> "$logfile"
          # 用 [p] 技巧避免匹配到自身命令行
          pkill -9 -f '[p]ts-core' 2>/dev/null
          pkill -9 -x phoronix-test-suite 2>/dev/null
          # 截断兜底：若还有残留进程在写，至少别撑爆磁盘
          tail -c 10485760 "$logfile" > "$logfile.tmp" 2>/dev/null && mv "$logfile.tmp" "$logfile"
        fi
      done
    ) >/dev/null 2>&1 &
    local watchdog=$!

    # batch-benchmark = 非交互批处理模式，自动采用默认选项（等价源方案的 default-benchmark）
    # HOME 下需要可写，PTS 结果落在 ~/.phoronix-test-suite
    # stdin 同样重定向，避免任何意外的交互提示挂住（同 deps 阶段的踩坑）
    #
    # ⚠️ **刻意不跑 pts/fio**：它是 PTS 里唯一有 1573 个变体组合的测试，且默认引擎
    #    IO_uring 在云 VM（1核2G）上必然失败——跑几十分钟一条成绩都产不出，只留下
    #    海量错误日志（就是上面那场事故的主因）。磁盘 IO 数据由本项目自建的 fio 精测
    #    覆盖（4K 随机读/写 + p99 + 顺序读/写），口径更统一、粒度更细。
    # ⚠️ 喂给 PTS 的 stdin 必须是**有效选项编号**（实测踩坑，2026-09-12 三台磁盘被写满）
    #    pts/openssl 的选项是**多选菜单**（RSA4096 / SHA256 / AES-128-GCM / ChaCha20 …，
    #    提示语 `** Multiple items can be selected, delimit by a comma. **`），
    #    它**没有默认值、必须输入有效编号**。三种喂法实测结果：
    #      </dev/null  → 读到 EOF，PTS 无限循环重印菜单（pts.log 涨到 29~37GB 写满三台盘）
    #      yes ''      → 空行是无效输入，同样无限重印（pts.log 600MB+，仍无成绩）
    #      yes '1'     → ✅ 每次都选中第 1 项，正常推进
    #    故这里用 `yes '1'`：对所有提问一律取第 1 个选项（即最基础的那一项）。
    #    另有日志看门狗兜底（见上），即便再遇异常路径也不会写满磁盘。
    #
    # 测试列表可由调度端覆盖（CB_PTS_TESTS，见 cmd_run 的 --pts-tests）：
    # 磁盘紧张的机器（如只剩 8G 的火山云）可只跑不占空间的 compress-7zip /
    # ramspeed / redis，跳过 build-linux-kernel（内核源码+编译产物，瞬时峰值约 27G）
    # 与 openssl（源码编译）——这会形成口径差异，需在汇总与文章中标注。
    local pts_tests="${CB_PTS_TESTS:-}"
    echo "PTS 测试列表: ${pts_tests:-（空）}"
    if [ -z "$pts_tests" ]; then
      echo "PTS 测试列表为空——拒绝隐式跑完整 5 项"
      echo "pts" >> "$out/failed-stages.txt"
      return 1
    fi
    # 记录本阶段开始时间，用于后面只认「本次运行新建」的 PTS 结果目录；
    # 不能简单取 test-results 下最新目录，否则阶段失败时会把旧 run 的结果错配进来。
    touch "$out/.pts-start"
    # 先声明；若在管道后再 `local`，会重置 PIPESTATUS（set -u 下直接报 unbound）。
    local pts_rc=0
    yes '1' | timeout "$TIMEOUT_PTS" phoronix-test-suite batch-benchmark $pts_tests
    # pipefail 下 `yes` 可能因 SIGPIPE 返回 141；这里只认 timeout/PTS 的状态。
    pts_rc=${PIPESTATUS[1]}
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    echo "=== PTS 退出码: $pts_rc ==="
  } > "$log" 2>&1

  rm -f "$out/pts-result-names.txt" "$out/pts-result-name.txt"
  # 只把本阶段开始后新建的结果目录记到 run 产物里，collect 时据此精确备份。
  # PTS 不同版本/测试拆分可能产生多个结果目录，因此记录全部，同时保留单值文件兼容旧逻辑。
  local pts_result_dirs
  pts_result_dirs=$(find "$HOME/.phoronix-test-suite/test-results" -mindepth 1 -maxdepth 1 \
    -type d -newer "$out/.pts-start" 2>/dev/null | sort)
  if [ -n "$pts_result_dirs" ]; then
    printf '%s\n' "$pts_result_dirs" | while IFS= read -r d; do basename "$d"; done > "$out/pts-result-names.txt"
    tail -1 "$out/pts-result-names.txt" > "$out/pts-result-name.txt"
  fi
  rm -f "$out/.pts-start"

  # 7-Zip 是 PTS 里最稳定可比的单项，提取出来做横向对比
  # ⚠️ 必须像 summarize.py 那样「剥离 ANSI + 放宽窗口」：
  #   ① PTS 输出在样本与 Average 之间有**空行**，`grep -A4` 取不到（实测漏掉）；
  #   ② 有 pty 时输出带 ANSI 颜色码（`Average: \x1b[1;34m...`），`Average: [0-9]` 失配。
  local txt_clean
  txt_clean=$(sed 's/\x1b\[[0-9;]*[A-Za-z]//g' "$log" 2>/dev/null)
  local z d
  z=$(printf '%s\n' "$txt_clean" | awk '/Compression Rating:/{f=1} f&&/Average:/{print $2; exit}')
  d=$(printf '%s\n' "$txt_clean" | awk '/Decompression Rating:/{f=1} f&&/Average:/{print $2; exit}')
  [ -n "$z" ] && metric "$out" "pts_7zip_compress_mips" "$z" "MIPS"
  [ -n "$d" ] && metric "$out" "pts_7zip_decompress_mips" "$d" "MIPS"

  # redis / 内核编译成绩也落到 metrics.tsv，保证远端阶段完整性判断
  # 与 Python 解析层使用同一份数据；否则会误判 standard/full 为 partial。
  local r k
  r=$(printf '%s\n' "$txt_clean" | awk '/SET - Parallel Connections: 50:/{f=1} f&&/Average:/{print $2; exit}')
  [ -n "$r" ] && metric "$out" "pts_redis_rps" "$r" "RPS"
  k=$(printf '%s\n' "$txt_clean" | awk 'tolower($0) ~ /build-linux-kernel|build linux kernel|linux kernel/ {f=1} f&&/Average:/{print $2; exit}')
  [ -n "$k" ] && metric "$out" "pts_kernel_build_sec" "$k" "s"

  # ⚠️ 必须校验是否真出成绩：batch 模式未配置时 PTS 会「下载安装完就退出」，
  #    **退出码依然是 0**，只看退出码会误判成功（首轮三台全中）。
  #    有效的 PTS 结果日志里必有 `Average: <数字>` 行（剥离 ANSI 后再判断）。
  local pts_has_output=0
  if printf '%s\n' "$txt_clean" | grep -qE 'Average:[[:space:]]*[0-9.]+'; then
    pts_has_output=1
  fi

  # 对已解析的测试做逐项校验；只跑了一部分不能报 ok。
  # openssl / ramspeed 目前没有结构化指标解析，不能据此判定缺失，只保留日志。
  local pts_missing="" t
  for t in $pts_tests; do
    case "$t" in
      compress-7zip)
        if [ -z "$(metric_value "$out" pts_7zip_compress_mips)" ] || \
           [ -z "$(metric_value "$out" pts_7zip_decompress_mips)" ]; then
          pts_missing="$pts_missing $t"
        fi
        ;;
      redis)
        [ -n "$(metric_value "$out" pts_redis_rps)" ] || pts_missing="$pts_missing $t"
        ;;
      build-linux-kernel)
        [ -n "$(metric_value "$out" pts_kernel_build_sec)" ] || pts_missing="$pts_missing $t"
        ;;
    esac
  done
  pts_missing="${pts_missing# }"

  local pts_metric_count=0
  if [ -f "$out/metrics.tsv" ]; then
    pts_metric_count=$(awk -F'\t' '$1 ~ /^pts_/ { n++ } END { print n + 0 }' "$out/metrics.tsv")
  fi

  if [ "$pts_has_output" -eq 0 ]; then
    if [ "${pts_rc:-1}" -ne 0 ]; then
      c_warn "PTS 退出码 ${pts_rc}（超时或测试失败），日志: pts.log"
    else
      c_warn "PTS 未产出任何测试成绩（日志: pts.log）——请检查 batch 模式配置"
    fi
    grep -q 'batch mode must first be configured' "$log" && \
      c_warn "  确认原因：日志含 'The batch mode must first be configured'"
    echo "pts" >> "$out/failed-stages.txt"
  elif [ "${pts_rc:-1}" -ne 0 ] || [ "$pts_metric_count" -eq 0 ] || [ -n "$pts_missing" ]; then
    c_warn "PTS 部分完成：退出码=${pts_rc:-?}，已解析指标=${pts_metric_count} 项，缺: ${pts_missing:-无（但无结构化指标）}"
    echo "pts" >> "$out/partial-stages.txt"
  else
    clear_failed "$out" pts
    c_ok "PTS 完成（日志: pts.log，已检出成绩）"
  fi

  case " $pts_tests " in
    *" openssl "*|*" ramspeed "*)
      c_info "PTS openssl/ramspeed 目前只保留原始日志，不参与结构化指标完整性判定"
      ;;
  esac
}

# fio 4K 精测：direct=1, bs=4k, iodepth=32, numjobs=4, runtime=60s，记录 IOPS 与 p99
stage_fio() {
  local out="$1"
  need_sudo
  local log="$out/fio.log"
  local dir="$out/fio"
  rm -f "$out"/fio-4k-randread.json "$out"/fio-4k-randwrite.json \
    "$out"/fio-seq-read.json "$out"/fio-seq-write.json
  mkdir -p "$dir"

  # 依据磁盘余量决定测试文件大小（每 job 一个文件，共 numjobs 份）
  local avail_gb size="1G" jobs=4
  avail_gb=$(df -BG --output=avail "$out" | tail -1 | tr -dc '0-9')
  if [ "${avail_gb:-0}" -lt 20 ]; then
    size="1G"; c_warn "磁盘余量 ${avail_gb}G < 20G，fio 测试文件降为 1G×${jobs}"
  elif [ "${avail_gb:-0}" -lt 40 ]; then
    size="2G"
  else
    size="4G"
  fi

  {
    echo "=== fio 磁盘精测 $(date -Is)（size=$size numjobs=$jobs）==="
    echo "--- 4K 随机读 ---"
    timeout "$TIMEOUT_FIO" fio --name=4k-randread --directory="$dir" --filename_format='fiotest.$jobnum' \
        --size="$size" --ioengine=libaio --direct=1 --bs=4k --iodepth=32 --numjobs="$jobs" \
        --rw=randread --runtime=60 --time_based --group_reporting \
        --percentile_list=50:99:99.9 --output-format=json --output="$out/fio-4k-randread.json"
    echo "退出码=$?"
    jq -r '.jobs[0] | "  IOPS=\(.read.iops) BW=\(.read.bw/1024)MiB/s p99=\(.read.clat_ns.percentile["99.000000"]/1000)us"' \
        "$out/fio-4k-randread.json" 2>/dev/null

    echo "--- 4K 随机写 ---"
    timeout "$TIMEOUT_FIO" fio --name=4k-randwrite --directory="$dir" --filename_format='fiotest.$jobnum' \
        --size="$size" --ioengine=libaio --direct=1 --bs=4k --iodepth=32 --numjobs="$jobs" \
        --rw=randwrite --runtime=60 --time_based --group_reporting \
        --percentile_list=50:99:99.9 --output-format=json --output="$out/fio-4k-randwrite.json"
    echo "退出码=$?"
    jq -r '.jobs[0] | "  IOPS=\(.write.iops) BW=\(.write.bw/1024)MiB/s p99=\(.write.clat_ns.percentile["99.000000"]/1000)us"' \
        "$out/fio-4k-randwrite.json" 2>/dev/null

    echo "--- 顺序读 1M ---"
    timeout "$TIMEOUT_FIO" fio --name=seq-read --directory="$dir" --filename_format='fiotest.$jobnum' \
        --size="$size" --ioengine=libaio --direct=1 --bs=1m --iodepth=16 --numjobs="$jobs" \
        --rw=read --runtime=60 --time_based --group_reporting \
        --output-format=json --output="$out/fio-seq-read.json"
    echo "退出码=$?"
    jq -r '.jobs[0] | "  BW=\(.read.bw/1024)MiB/s IOPS=\(.read.iops)"' "$out/fio-seq-read.json" 2>/dev/null

    echo "--- 顺序写 1M ---"
    timeout "$TIMEOUT_FIO" fio --name=seq-write --directory="$dir" --filename_format='fiotest.$jobnum' \
        --size="$size" --ioengine=libaio --direct=1 --bs=1m --iodepth=16 --numjobs="$jobs" \
        --rw=write --runtime=60 --time_based --group_reporting \
        --output-format=json --output="$out/fio-seq-write.json"
    echo "退出码=$?"
    jq -r '.jobs[0] | "  BW=\(.write.bw/1024)MiB/s IOPS=\(.write.iops)"' "$out/fio-seq-write.json" 2>/dev/null

    echo "=== 清理测试文件 $(date -Is) ==="
    rm -rf "$dir"
    echo "已删除 $dir"
  } > "$log" 2>&1

  local rr="$out/fio-4k-randread.json" rw="$out/fio-4k-randwrite.json"
  if [ -s "$rr" ]; then
    metric "$out" "fio_4k_randread_iops"  "$(jq -r '.jobs[0].read.iops // empty' "$rr" 2>/dev/null)" "IOPS"
    metric "$out" "fio_4k_randread_p99_us" "$(jq -r '.jobs[0].read.clat_ns.percentile["99.000000"] / 1000 // empty' "$rr" 2>/dev/null)" "us"
    metric "$out" "fio_4k_randread_p999_us" "$(jq -r '.jobs[0].read.clat_ns.percentile["99.900000"] / 1000 // empty' "$rr" 2>/dev/null)" "us"
    metric "$out" "fio_4k_randread_bw"    "$(jq -r '.jobs[0].read.bw / 1024 // empty' "$rr" 2>/dev/null)" "MiB/s"
  fi
  if [ -s "$rw" ]; then
    metric "$out" "fio_4k_randwrite_iops"  "$(jq -r '.jobs[0].write.iops // empty' "$rw" 2>/dev/null)" "IOPS"
    metric "$out" "fio_4k_randwrite_p99_us" "$(jq -r '.jobs[0].write.clat_ns.percentile["99.000000"] / 1000 // empty' "$rw" 2>/dev/null)" "us"
    metric "$out" "fio_4k_randwrite_p999_us" "$(jq -r '.jobs[0].write.clat_ns.percentile["99.900000"] / 1000 // empty' "$rw" 2>/dev/null)" "us"
    metric "$out" "fio_4k_randwrite_bw"    "$(jq -r '.jobs[0].write.bw / 1024 // empty' "$rw" 2>/dev/null)" "MiB/s"
  fi
  [ -s "$out/fio-seq-read.json" ] && \
    metric "$out" "fio_seq_read_mib_s"  "$(jq -r '.jobs[0].read.bw / 1024 // empty' "$out/fio-seq-read.json" 2>/dev/null)" "MiB/s"
  [ -s "$out/fio-seq-write.json" ] && \
    metric "$out" "fio_seq_write_mib_s" "$(jq -r '.jobs[0].write.bw / 1024 // empty' "$out/fio-seq-write.json" 2>/dev/null)" "MiB/s"

  # ⚠️ 失败检测：4 个 JSON 必须齐全；只检查 4K 两项会把顺序读写失败静默放过。
  local fio_missing=""
  local f q
  for f in fio-4k-randread.json fio-4k-randwrite.json fio-seq-read.json fio-seq-write.json; do
    case "$f" in
      fio-4k-randread.json)
        q='.jobs[0].read.iops and .jobs[0].read.clat_ns.percentile["99.000000"] and .jobs[0].read.clat_ns.percentile["99.900000"]'
        ;;
      fio-4k-randwrite.json)
        q='.jobs[0].write.iops and .jobs[0].write.clat_ns.percentile["99.000000"] and .jobs[0].write.clat_ns.percentile["99.900000"]'
        ;;
      fio-seq-read.json) q='.jobs[0].read.bw' ;;
      fio-seq-write.json) q='.jobs[0].write.bw' ;;
    esac
    if [ ! -s "$out/$f" ] || ! jq -e "$q" "$out/$f" >/dev/null 2>&1; then
      fio_missing="$fio_missing $f"
    fi
  done
  if [ -n "$fio_missing" ]; then
    c_warn "fio 未产出完整 JSON，缺失:$fio_missing（见 $log）"
    echo "fio" >> "$out/failed-stages.txt"
  else
    clear_failed "$out" fio
    c_ok "fio 精测完成（测试文件已清理）"
  fi
}

stage_sysbench() {
  local out="$1"
  local log="$out/sysbench.log"
  need_sudo
  local nproc_val; nproc_val=$(nproc)
  {
    echo "=== sysbench $(date -Is) ==="
    echo "--- CPU 单线程 60s ---"
    timeout "$TIMEOUT_SYSBENCH" sysbench cpu --cpu-max-prime=20000 --threads=1 --time=60 run
    echo
    echo "--- CPU 全核(${nproc_val}) 60s ---"
    timeout "$TIMEOUT_SYSBENCH" sysbench cpu --cpu-max-prime=20000 --threads="$nproc_val" --time=60 run
    echo
    echo "--- 内存带宽（1M 块，单线程，60s）---"
    timeout "$TIMEOUT_SYSBENCH" sysbench memory --memory-block-size=1M --memory-total-size=100G --threads=1 --time=60 run
    echo
    echo "--- 内存带宽（1M 块，全核，60s）---"
    timeout "$TIMEOUT_SYSBENCH" sysbench memory --memory-block-size=1M --memory-total-size=100G --threads="$nproc_val" --time=60 run
  } > "$log" 2>&1

  # 按脚本写入的段标题切片，和 bench_metrics.parse_sysbench 保持同一口径。
  # 不能全局 grep 前两个值：CPU 单核段失败时，多核/内存结果会错位顶替成单核成绩。
  local s1 s2 m1 m2
  s1=$(awk '/--- CPU 单线程 60s ---/{f=1} /--- CPU 全核/{f=0} f && /events per second:/{print $NF; exit}' "$log")
  s2=$(awk '/--- CPU 全核/{f=1} /--- 内存带宽/{f=0} f && /events per second:/{print $NF; exit}' "$log")
  m1=$(awk '/--- 内存带宽（1M 块，单线程，60s）---/{f=1} /--- 内存带宽（1M 块，全核/{f=0} f && /MiB\/sec/{print $1; exit}' "$log")
  m2=$(awk '/--- 内存带宽（1M 块，全核/{f=1} f && /MiB\/sec/{print $1; exit}' "$log")
  [ -n "$s1" ] && metric "$out" "sysbench_cpu_single_eps" "$s1" "events/s"
  [ -n "$s2" ] && metric "$out" "sysbench_cpu_multi_eps"  "$s2" "events/s"
  [ -n "$m1" ] && metric "$out" "sysbench_mem_single_mib_s" "$m1" "MiB/s"
  [ -n "$m2" ] && metric "$out" "sysbench_mem_multi_mib_s"  "$m2" "MiB/s"

  # ⚠️ 失败检测：连 events per second 都解析不到说明 sysbench 根本没跑起来
  #    （2026-09-13 复查补充），不能静默报「完成」。
  if [ -z "$s1" ] || [ -z "$s2" ] || [ -z "$m1" ] || [ -z "$m2" ]; then
    c_warn "sysbench 产出不完整（CPU/内存有空项），见 $log"
    echo "sysbench" >> "$out/failed-stages.txt"
  else
    clear_failed "$out" sysbench
    c_ok "sysbench 完成（单核 ${s1} / 全核 ${s2} events/s）"
  fi
}

# 超售检测：全核满载 20 分钟，同时采样 %steal（这是判断邻居抢占的核心指标）
stage_stress() {
  local out="$1"
  need_sudo
  local log="$out/stress-ng.log"
  local mplog="$out/mpstat.log"
  # 压测时长由档位决定（quick 5m / standard 10m / full 20m）。
  # 5 分钟足以暴露「持续被邻居抢占」——超售很少是瞬时的，但也很少需要 20 分钟才显形。
  local mins="${CB_STRESS_MINS:-20}"
  {
    echo "=== stress-ng 全核 ${mins}m + mpstat 采样 $(date -Is) ==="
    echo "nproc=$(nproc)"
    # ⚠️ 实测踩坑（天翼云）：部分云厂商的 VM 会屏蔽 AVX/AVX2/FMA/AVX-512 指令集
    #    （该机 CPU flags 只有 sse4_2/aes，无任何 avx），而 stress-ng 默认
    #    cpu-method=all 会执行 AVX 指令 → SIGILL(ILL_ILLOPN) → 0.01s 即
    #    `failed: 2: cpu`、bogo ops 全零，连带 mpstat 只采到表头（%steal 数据全失）。
    #    回退到纯整数运算 int32（不依赖 SIMD），保证 %steal 仍可测量。
    local METHOD=""
    if ! timeout 60 stress-ng --cpu "$(nproc)" --timeout 3s --metrics-brief >/dev/null 2>&1; then
      METHOD="--cpu-method int32"
      echo "[WARN] 默认 cpu-method 不可用（疑似 VM 屏蔽 AVX），回退 --cpu-method int32"
      echo "  该机 /proc/cpuinfo 含 avx: $(grep -qw avx /proc/cpuinfo && echo 是 || echo 否)"
    fi
    echo "stress-ng 实际参数: --cpu $(nproc) $METHOD --timeout ${mins}m"
    mpstat 1 > "$mplog" 2>&1 &
    local mpid=$!
    timeout "$TIMEOUT_STRESS" stress-ng --cpu "$(nproc)" $METHOD --timeout "${mins}m" --metrics-brief
    echo "stress-ng 退出码=$?"
    kill "$mpid" 2>/dev/null
    wait "$mpid" 2>/dev/null
    echo "=== 结束 $(date -Is) ==="
  } > "$log" 2>&1

  # mpstat: 第 3 列是 %usr ... 最后一列是 %steal（不同版本列数可能不同，按表头定位）
  if [ -s "$mplog" ]; then
    local steal_idx idle_idx
    steal_idx=$(awk '/%steal/{for(i=1;i<=NF;i++) if($i=="%steal") print i; exit}' "$mplog")
    idle_idx=$(awk '/%idle/{for(i=1;i<=NF;i++) if($i=="%idle") print i; exit}' "$mplog")
    if [ -n "${steal_idx:-}" ]; then
      # 跳过表头与汇总行（"Average:"），取运行中的样本
      local avg_steal max_steal avg_idle=""
      avg_steal=$(awk -v c="$steal_idx" 'NR>3 && $1!="Average:" && $c ~ /^[0-9.]+$/ {s+=$c; n++} END {if(n) printf "%.2f", s/n}' "$mplog")
      max_steal=$(awk -v c="$steal_idx" 'NR>3 && $1!="Average:" && $c ~ /^[0-9.]+$/ {if($c>m) m=$c; n++} END {if(n) printf "%.2f", m+0}' "$mplog")
      if [ -n "${idle_idx:-}" ]; then
        avg_idle=$(awk -v c="$idle_idx" 'NR>3 && $1!="Average:" && $c ~ /^[0-9.]+$/ {s+=$c; n++} END {if(n) printf "%.2f", s/n}' "$mplog")
      fi
      if [ -n "$avg_steal" ]; then
        metric "$out" "stress_avg_steal_pct" "$avg_steal" "%"
        metric "$out" "stress_max_steal_pct" "$max_steal" "%"
        [ -n "$avg_idle" ] && metric "$out" "stress_avg_idle_pct" "$avg_idle" "%"
        c_ok "stress-ng 完成（平均 %steal=${avg_steal}% / 峰值 ${max_steal}%）"
      else
        c_warn "mpstat 没有有效样本（可能只采到表头），见 $mplog"
        echo "stress" >> "$out/failed-stages.txt"
      fi
    else
      c_warn "未能从 mpstat 解析 %steal，见 $mplog"
      echo "stress" >> "$out/failed-stages.txt"
    fi
  else
    c_warn "mpstat 未产出日志，见 $mplog"
    echo "stress" >> "$out/failed-stages.txt"
  fi
  if ! grep -q 'stress-ng 退出码=0' "$log" 2>/dev/null; then
    c_warn "stress-ng 未正常退出，见 $log"
    echo "stress" >> "$out/failed-stages.txt"
  fi
  # stress-ng bogo ops 作为满载下的算力参考
  # 注意 stress-ng metrics 行格式（前缀带 pid）：
  #   stress-ng: metrc: [1116252] cpu 30691 8.00 15.98 0.00 3836.36 1921.12
  #   $1=stress-ng:  $2=metrc:  $3=[pid]  $4=cpu  $5=bogo总ops  $6=real时间
  #   $7=usr时间  $8=sys时间  $9=bogo ops/s(real)  $10=bogo ops/s(usr+sys)
  # 取 $9（按 real time 的 ops/s）。
  local bogo
  bogo=$(grep -oE '^.*\bcpu\s+[0-9.]+\s+[0-9.]+\s+[0-9.]+\s+[0-9.]+\s+[0-9.]+\s+[0-9.]+' "$log" 2>/dev/null | awk '{print $9}' | head -1)
  [ -n "$bogo" ] && metric "$out" "stress_bogo_ops" "$bogo" "bogo ops/s"
  return 0
}

stage_net() {
  local out="$1"
  local log="$out/speedtest.log"
  rm -f "$out/speedtest.json" "$out/speedtest.txt" "$out/speedtest-result.json"
  {
    echo "=== 公网 Speedtest $(date -Is) ==="
    cd "$out" || exit 1
    # Ookla 官方 CLI（轻量套餐带宽受限，结果会顶到套餐上限；记录标称带宽用于解释）
    if [ ! -x ./speedtest ]; then
      # 下载走 cb_curl（检测本机 mihomo 代理）；测速本身连国内节点、直连更快
      cb_curl "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz" -o st.tgz \
        && tar xzf st.tgz speedtest 2>/dev/null && rm -f st.tgz
    fi
    if [ -x ./speedtest ]; then
      # ⚠️ 不要接 `head -40`：speedtest 输出超 40 行时 head 会提前关管道，
      #    speedtest 收到 SIGPIPE 被杀、JSON 不完整（2026-09-13 复查发现的风险）。
      #    输出全部落盘，由下方按行提取 result 行解析（EULA 文本会被跳过）。
      timeout "$TIMEOUT_NET" ./speedtest --accept-license --accept-gdpr --format=json > speedtest.json 2>&1
      echo "speedtest 退出码=$?"
    else
      echo "Ookla CLI 下载失败，回退 speedtest-cli"
      timeout 120 cb_curl https://raw.githubusercontent.com/sivel/speedtest-cli/master/speedtest.py -o st.py \
        && timeout "$TIMEOUT_NET" python3 st.py --simple 2>&1 | tee speedtest.txt
    fi
    echo "=== 结束 $(date -Is) ==="
  } > "$log" 2>&1

  # ⚠️ Ookla CLI 首次运行会先在 stdout 打印一整段 EULA 文本，JSON 结果在其后的
  #    某一行——直接对整个文件 jq 会解析失败（首轮三台 speedtest.json 全被误判为
  #    「未产出可用 JSON」，实际数据完好）。故按行提取 result 行再解析。
  local sfile="$out/speedtest.json"
  [ -s "$sfile" ] || sfile="$log"
  local jline
  jline=$(grep -o '{"type":"result".*}' "$sfile" 2>/dev/null | tail -1)
  if [ -n "$jline" ] && printf '%s' "$jline" | jq -e '.download.bandwidth and .upload.bandwidth' >/dev/null 2>&1; then
    printf '%s\n' "$jline" > "$out/speedtest-result.json"
    metric "$out" "speedtest_down_mbps"  "$(printf '%s' "$jline" | jq -r '.download.bandwidth / 125000 // empty' 2>/dev/null)" "Mbps"
    metric "$out" "speedtest_up_mbps"    "$(printf '%s' "$jline" | jq -r '.upload.bandwidth / 125000 // empty' 2>/dev/null)" "Mbps"
    metric "$out" "speedtest_latency_ms" "$(printf '%s' "$jline" | jq -r '.ping.latency // empty' 2>/dev/null)" "ms"
    metric "$out" "speedtest_jitter_ms"  "$(printf '%s' "$jline" | jq -r '.ping.jitter // empty' 2>/dev/null)" "ms"
    metric "$out" "speedtest_server"     "$(printf '%s' "$jline" | jq -r '.server.name // empty' 2>/dev/null)" ""
    clear_failed "$out" net
    c_ok "Speedtest 完成"
  elif [ -s "$out/speedtest.txt" ]; then
    # speedtest-cli --simple 的回退路径：文本解析，不能因为没 JSON 就把 net 判失败
    local sdown sup sping
    sdown=$(awk -F': ' '/^Download:/{print $2}' "$out/speedtest.txt" | awk '{print $1}' | tail -1)
    sup=$(awk -F': ' '/^Upload:/{print $2}' "$out/speedtest.txt" | awk '{print $1}' | tail -1)
    sping=$(awk -F': ' '/^Ping:/{print $2}' "$out/speedtest.txt" | awk '{print $1}' | tail -1)
    [ -n "$sdown" ] && metric "$out" "speedtest_down_mbps" "$sdown" "Mbps"
    [ -n "$sup" ] && metric "$out" "speedtest_up_mbps" "$sup" "Mbps"
    [ -n "$sping" ] && metric "$out" "speedtest_latency_ms" "$sping" "ms"
    clear_failed "$out" net
    c_ok "Speedtest 完成（speedtest-cli 回退）"
  else
    c_warn "Speedtest 未取到 result 行，见 $log"
    echo "net" >> "$out/failed-stages.txt"
  fi
}

# 远端入口：在 tmux 里按阶段顺序执行，并写 run-status.tsv / stage-status.tsv
remote_exec() {
  local out="$1" stages="$2"
  mkdir -p "$out"
  cd "$out" || exit 1
  echo "$$" > "$out/exec.pid"
  rm -f "$out/DONE" "$out/failed-stages.txt" "$out/partial-stages.txt" "$out/metrics.tsv"
  printf 'stage\tstatus\tstarted_at\tfinished_at\n' > "$out/stage-status.tsv"
  local run_started; run_started=$(date -Is)
  write_run_status "$out" running "$run_started" "" "$stages"

  local failed=0 partial=0 ok=0
  for s in $stages; do
    local stage_started; stage_started=$(date -Is)
    echo "[$stage_started] >>> 阶段开始: $s" >> "$out/progress.log"
    case "$s" in
      env)      stage_env "$out" ;;
      deps)     stage_deps "$out" ;;
      yabs)     stage_yabs "$out" ;;
      pts)      stage_pts "$out" ;;
      fio)      stage_fio "$out" ;;
      sysbench) stage_sysbench "$out" ;;
      stress)   stage_stress "$out" ;;
      net)      stage_net "$out" ;;
      *) c_warn "未知阶段: $s"; echo "$s" >> "$out/failed-stages.txt"; false ;;
    esac
    local rc=$?
    local stage_status=ok
    if [ "$rc" -ne 0 ] || grep -qxF "$s" "$out/failed-stages.txt" 2>/dev/null; then
      stage_status=failed
      failed=$((failed + 1))
    elif grep -qxF "$s" "$out/partial-stages.txt" 2>/dev/null; then
      stage_status=partial
      partial=$((partial + 1))
    else
      ok=$((ok + 1))
    fi
    local stage_finished; stage_finished=$(date -Is)
    append_stage_status "$out" "$s" "$stage_status" "$stage_started" "$stage_finished"
    echo "[$stage_finished] <<< 阶段结束: $s ($stage_status)" >> "$out/progress.log"
  done

  local run_finished run_status
  run_finished=$(date -Is)
  if [ "$failed" -eq 0 ] && [ "$partial" -eq 0 ]; then
    run_status=ok
    touch "$out/DONE"
  elif [ "$ok" -eq 0 ] && [ "$partial" -eq 0 ]; then
    run_status=failed
  else
    run_status=partial
  fi
  write_run_status "$out" "$run_status" "$run_started" "$run_finished" "$stages"
  echo "[$run_finished] === 运行结束: $run_status ===" >> "$out/progress.log"
}

# 多次采样：在远端按顺序执行 N 个独立 run，避免并发互相污染。
# 单次 run 仍走 cmd_run 的原路径；这里只处理 repeat > 1。
run_batch() {
  local name="$1" target="$2" profile="$3" stages="$4" at="$5"
  local pts_tests="$6" pts_proxy="$7" stress_mins="$8" repeat="$9"
  local need_pts="${10}" need_kernel="${11}"

  local batch_id batch_created
  batch_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
  batch_created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local remote_batch="$REMOTE_BATCHES/$batch_id"
  local -a run_ids=()
  local i run_id
  for ((i = 1; i <= repeat; i++)); do
    run_ids+=("$batch_id-r$(printf '%02d' "$i")")
  done
  local run_ids_str="${run_ids[*]}"
  local first_run="${run_ids[0]}"
  local remote_out="$REMOTE_RUNS/$first_run/out"

  c_info "批次 ID: $batch_id（$repeat 次独立 run，顺序执行）"
  c_info "运行 ID: $run_ids_str"

  # 上传脚本并创建批次/首个 run 目录。
  rsh "$target" "mkdir -p $REMOTE_BASE $REMOTE_BATCHES $remote_batch $remote_out && cat > $REMOTE_BASE/cloud-benchmark.sh && chmod +x $REMOTE_BASE/cloud-benchmark.sh" \
    < "${BASH_SOURCE[0]}" || die "上传脚本失败"

  # 批次清单；collect/status 都从这里取 run_ids。
  local tmp_manifest; tmp_manifest="$(mktemp /tmp/cb-batch-XXXXXX.tsv)"
  {
    printf 'batch_id\t%s\n' "$batch_id"
    printf 'name\t%s\n' "$name"
    printf 'profile\t%s\n' "$profile"
    printf 'repeat\t%s\n' "$repeat"
    printf 'stages\t%s\n' "$stages"
    printf 'created_at\t%s\n' "$batch_created"
    printf 'run_ids\t%s\n' "$run_ids_str"
  } > "$tmp_manifest"
  rsh "$target" "cat > $remote_batch/batch.tsv" < "$tmp_manifest" || die "上传批次清单失败"
  rm -f "$tmp_manifest"

  local avail
  avail=$(rsh "$target" "df -BG --output=avail $remote_out | tail -1 | tr -dc '0-9'") \
    || die "无法连接 $target（SSH 失败）"
  [ -n "$avail" ] || die "无法读取 $target 的磁盘余量（df 无输出）"
  if [ "$need_kernel" -eq 1 ] && [ "$avail" -lt 32 ]; then
    die "远端磁盘剩余 ${avail}G，full 档内核编译峰值约 27G；请清理磁盘，或用 --pts-tests 跳过 build-linux-kernel"
  fi
  if [ "$avail" -lt 5 ]; then
    die "远端磁盘剩余 ${avail}G < 5G，先清理再跑"
  fi
  [ "$avail" -lt 10 ] && c_warn "远端磁盘仅剩 ${avail}G，跑 PTS 时请留意"
  c_ok "远端磁盘剩余 ${avail}G"

  local tmp_start; tmp_start="$(mktemp /tmp/cb-start-XXXXXX.sh)"
  {
    echo '#!/bin/bash'
    echo "export CB_PROFILE='$profile'"
    echo "export CB_NEED_PTS='$need_pts'"
    echo "export CB_NEED_KERNEL_DEPS='$need_kernel'"
    [ -n "$pts_tests" ] && echo "export CB_PTS_TESTS='$pts_tests'"
    [ -n "$pts_proxy" ] && echo "export CB_PTS_PROXY='$pts_proxy'"
    echo "export CB_STRESS_MINS='$stress_mins'"
    echo "cd $REMOTE_BASE || exit 1"
    echo "mkdir -p $remote_batch"
    echo "echo '$batch_id' > $REMOTE_LATEST_BATCH"
    if [ -n "$at" ]; then
      cat <<EOF
t=\$(date -d 'today $at' +%s)
n=\$(date +%s)
[ "\$t" -le "\$n" ] && t=\$(date -d 'tomorrow $at' +%s)
w=\$((t-n))
echo "[\$(date -Is)] [定时] 等待 \${w}s，至远端时间 $at 开跑" | tee -a $remote_batch/driver.log
sleep "\$w"
EOF
    fi
    echo "for run_id in $run_ids_str; do"
    echo "  out=$REMOTE_RUNS/\$run_id/out"
    echo '  mkdir -p "$out"'
    echo '  echo $$ > "$out/start.pid"'
    echo "  echo \"\$run_id\" > $REMOTE_LATEST"
    echo '  export CB_RUN_ID="$run_id"'
    echo "  ./cloud-benchmark.sh __exec --out \"\$out\" --stages \"$stages\" 2>&1 | tee -a \"\$out/driver.log\""
    echo "done"
  } > "$tmp_start"

  rsh "$target" "cat > $REMOTE_BASE/start.sh && chmod +x $REMOTE_BASE/start.sh" < "$tmp_start" \
    || die "上传启动脚本失败"
  rm -f "$tmp_start"

  local launch
  launch="cd $REMOTE_BASE || exit 1
chmod +x start.sh
for f in $REMOTE_RUNS/*/out/start.pid; do
  [ -f \"\$f\" ] || continue
  p=\$(cat \"\$f\" 2>/dev/null) || continue
  case \"\$p\" in ''|*[!0-9]*) continue ;; esac
  kill \"\$p\" 2>/dev/null || true
done
pkill -f '[c]loud-benchmark.sh __exec' 2>/dev/null || true
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t $TMUX_SESSION 2>/dev/null
  tmux new-session -d -s $TMUX_SESSION './start.sh'
  echo 'LAUNCH=tmux 会话=$TMUX_SESSION'
else
  setsid nohup ./start.sh >/dev/null 2>&1 </dev/null &
  echo \"LAUNCH=nohup pid=\$!\"
fi"
  rsh "$target" "$launch" || die "后台启动失败"

  c_ok "已启动批次（远端顺序执行，SSH 断连不中断）"
  c_info "批次 ID: $batch_id"
  c_info "查看进度: $0 status --name $name --batch $batch_id"
  c_info "回收结果: $0 collect --name $name --batch $batch_id"
}

cmd_run() {
  local name="" profile="" stages="" at="" pts_tests="" pts_proxy="" stress_mins="" force="" repeat=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --name)        name="$2"; shift 2 ;;
      --profile)     profile="$2"; shift 2 ;;
      --stages)      stages="$(echo "$2" | tr ',' ' ')"; shift 2 ;;
      --at)          at="$2"; shift 2 ;;
      --pts-tests)   pts_tests="$2"; shift 2 ;;
      --pts-proxy)   pts_proxy="$2"; shift 2 ;;
      --stress-mins) stress_mins="$2"; shift 2 ;;
      --repeat)
        [ $# -ge 2 ] || die "--repeat 需要正整数"
        repeat="$2"; shift 2 ;;

      --force)       force=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$name" ] || die "用法: $0 run --name <主机名> [--profile quick|standard|full] [--stages env,deps,...] [--at HH:MM] [--pts-tests \"...\"] [--stress-mins N] [--repeat N] [--force]"

  if [ -n "$at" ]; then
    case "$at" in
      [0-2][0-9]:[0-5][0-9]) ;;
      *) die "非法 --at: $at（格式 HH:MM）" ;;
    esac
    [ "${at%%:*}" -le 23 ] || die "非法 --at: $at（小时必须 00-23）"
  fi
  if [ -n "$pts_proxy" ] && ! printf '%s\n' "$pts_proxy" | grep -Eq '^[A-Za-z0-9._:-]+$'; then
    die "非法 --pts-proxy: $pts_proxy（格式 host:port）"
  fi

  # 档位展开为默认值；显式传入的参数优先
  # （: "${var:=default}" 仅在 var 为空时赋值，故命令行给的值得以保留）
  : "${profile:=quick}"
  case "$profile" in
    quick)    : "${stages:=$PROFILE_QUICK}";    : "${pts_tests:=$PTS_QUICK}";    : "${stress_mins:=$STRESS_QUICK}" ;;
    standard) : "${stages:=$PROFILE_STANDARD}"; : "${pts_tests:=$PTS_STANDARD}"; : "${stress_mins:=$STRESS_STANDARD}" ;;
    full)     : "${stages:=$ALL_STAGES}";       : "${pts_tests:=$PTS_FULL}";     : "${stress_mins:=$STRESS_FULL}" ;;
    *) die "未知档位: $profile（可选 quick / standard / full）" ;;
  esac
  case "$stress_mins" in
    ""|*[!0-9]*) die "非法 --stress-mins: $stress_mins（必须为正整数）" ;;
  esac
  [ "$stress_mins" -gt 0 ] || die "非法 --stress-mins: 必须大于 0"
  case "$repeat" in
    ""|*[!0-9]*) die "非法 --repeat: $repeat（必须为正整数）" ;;
  esac
  [ "$repeat" -gt 0 ] || die "非法 --repeat: 必须大于 0"

  if [ -n "$pts_tests" ]; then
    printf '%s\n' "$pts_tests" | grep -Eq '^[A-Za-z0-9._ -]+$' || \
      die "非法 --pts-tests: $pts_tests（只允许测试名、空格、点、下划线、短横线）"
    local t
    for t in $pts_tests; do
      case "$t" in
        -*) die "非法 --pts-tests: 测试名不能以 - 开头（$t）" ;;
      esac
    done
  fi

  local s
  for s in $stages; do
    case " $ALL_STAGES " in
      *" $s "*) ;;
      *) die "未知阶段: $s（可选: $ALL_STAGES）" ;;
    esac
  done
  # quick 档没有默认 PTS 列表；显式加 pts 时必须说明跑哪些测试，
  # 否则 stage_pts 会落到完整 5 项（含 87 分钟内核编译）。
  if [[ " $stages " == *" pts "* ]] && [ -z "$pts_tests" ]; then
    die "pts 阶段需要显式 --pts-tests（例如 --pts-tests \"compress-7zip redis\"）"
  fi
  local need_pts=0 need_kernel=0
  case " $stages " in *" pts "*) need_pts=1 ;; esac
  case " $pts_tests " in *" build-linux-kernel "*) need_kernel=1 ;; esac
  [ "$need_pts" -eq 0 ] && need_kernel=0

  local target; target="$(host_target "$name")"

  # 同一台机器上已有任务时不要静默覆盖；--force 才允许中断旧任务。
  # 除 tmux / __exec 外，还要检查 start.pid：无 tmux 时 --at 会先 sleep，
  # 这段时间没有 __exec 进程，只查 __exec 会漏掉。
  if rsh "$target" "
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t $TMUX_SESSION 2>/dev/null; then exit 0; fi
    if pgrep -f '[c]loud-benchmark.sh __exec' >/dev/null 2>&1; then exit 0; fi
    for f in $REMOTE_RUNS/*/out/start.pid; do
      [ -f \"\$f\" ] || continue
      p=\$(cat \"\$f\" 2>/dev/null) || continue
      case \"\$p\" in ''|*[!0-9]*) continue ;; esac
      kill -0 \"\$p\" 2>/dev/null && exit 0
    done
    exit 1
  "; then
    [ -n "$force" ] || die "远端已有评测任务在跑；确认要中断请加 --force"
  fi

  c_head "在 $name ($target) 上启动评测"
  case "$profile" in
    quick)    c_info "档位: quick（约 16 分钟）—— 覆盖轻量云最关心的：磁盘 p99 / 超售 %steal / 物理核识别" ;;
    standard) c_info "档位: standard（约 40 分钟）—— quick + YABS 交叉验证 + PTS（7-Zip/redis）" ;;
    full)     c_info "档位: full（约 2.5 小时）—— 全量，含 PTS 内核编译（轻量云一般用不到）" ;;
  esac
  c_info "阶段: $stages"
  c_info "压测时长: ${stress_mins} 分钟"
  [ -n "$at" ] && c_info "定时: 远端本地时间 $at 开跑（远端计算，本机可关机）"
  [ "$repeat" -gt 1 ] && c_info "采样: ${repeat} 次（远端顺序执行，避免并发互相污染）"

  # N>=2 走批次路径；N=1 保留原有单次 run 语义，避免改变旧用户行为。
  if [ "$repeat" -gt 1 ]; then
    run_batch "$name" "$target" "$profile" "$stages" "$at" \
      "$pts_tests" "$pts_proxy" "$stress_mins" "$repeat" \
      "$need_pts" "$need_kernel"
    return 0
  fi

  # 每次运行独立目录；远端 latest-run 指向最近一次，collect 默认取它。
  local run_id remote_run remote_out
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
  remote_run="$REMOTE_RUNS/$run_id"
  remote_out="$remote_run/out"
  c_info "运行 ID: $run_id"

  # 上传本脚本自身（路径不加引号，让远端的 ~ 正常展开）
  rsh "$target" "mkdir -p $REMOTE_BASE $remote_out && cat > $REMOTE_BASE/cloud-benchmark.sh && chmod +x $REMOTE_BASE/cloud-benchmark.sh" \
    < "${BASH_SOURCE[0]}" || die "上传脚本失败"

  # 前端检查：磁盘余量。检查实际产物目录所在文件系统，而不是固定的 /。
  # full 档的 build-linux-kernel 瞬时峰值约 27G；不满足时直接拒绝，避免写满生产盘。
  local avail
  avail=$(rsh "$target" "df -BG --output=avail $remote_out | tail -1 | tr -dc '0-9'") \
    || die "无法连接 $target（SSH 失败）"
  if [ -z "$avail" ]; then
    die "无法读取 $target 的磁盘余量（df 无输出）"
  fi
  if [ "$need_kernel" -eq 1 ] && [ "$avail" -lt 32 ]; then
    die "远端磁盘剩余 ${avail}G，full 档内核编译峰值约 27G；请清理磁盘，或用 --pts-tests 跳过 build-linux-kernel"
  fi
  if [ "$avail" -lt 5 ]; then
    die "远端磁盘剩余 ${avail}G < 5G，先清理再跑"
  fi
  [ "$avail" -lt 10 ] && c_warn "远端磁盘仅剩 ${avail}G，跑 PTS 时请留意"
  c_ok "远端磁盘剩余 ${avail}G"

  # 生成远端启动脚本（含可选定时等待），避免多层引号嵌套出错。
  # 定时用**远端**的 date 计算：本机与远端时区不一致也不会跑偏。
  local tmp_start; tmp_start="$(mktemp /tmp/cb-start-XXXXXX.sh)"
  {
    echo '#!/bin/bash'
    echo "export CB_RUN_ID='$run_id'"
    echo "export CB_PROFILE='$profile'"
    echo "export CB_NEED_PTS='$need_pts'"
    echo "export CB_NEED_KERNEL_DEPS='$need_kernel'"
    [ -n "$pts_tests" ] && echo "export CB_PTS_TESTS='$pts_tests'"
    [ -n "$pts_proxy" ] && echo "export CB_PTS_PROXY='$pts_proxy'"
    echo "export CB_STRESS_MINS='$stress_mins'"
    echo "cd $REMOTE_BASE || exit 1"
    echo "mkdir -p $remote_out"
    echo "echo \$\$ > $remote_out/start.pid"
    echo "echo '$run_id' > $REMOTE_LATEST"
    if [ -n "$at" ]; then
      cat <<EOF
t=\$(date -d 'today $at' +%s)
n=\$(date +%s)
[ "\$t" -le "\$n" ] && t=\$(date -d 'tomorrow $at' +%s)
w=\$((t-n))
echo "[\$(date -Is)] [定时] 等待 \${w}s，至远端时间 $at 开跑" | tee -a $remote_out/driver.log
sleep "\$w"
EOF
    fi
    echo "./cloud-benchmark.sh __exec --out $remote_out --stages \"$stages\" 2>&1 | tee -a $remote_out/driver.log"
  } > "$tmp_start"

  # 用 cat 重定向而非 rsync：轻量镜像常未装 rsync（天翼云实测 `rsync: command not found`）
  rsh "$target" "cat > $REMOTE_BASE/start.sh && chmod +x $REMOTE_BASE/start.sh" < "$tmp_start" \
    || die "上传启动脚本失败"
  rm -f "$tmp_start"

  # 后台启动：优先 tmux（便于 attach 观察），无 tmux 时回退 setsid+nohup
  # （轻量镜像常两者缺一：天翼云实测无 tmux、无 rsync）
  local launch
  launch="cd $REMOTE_BASE || exit 1
chmod +x start.sh
for f in $REMOTE_RUNS/*/out/start.pid; do
  [ -f \"\$f\" ] || continue
  p=\$(cat \"\$f\" 2>/dev/null) || continue
  case \"\$p\" in ''|*[!0-9]*) continue ;; esac
  kill \"\$p\" 2>/dev/null || true
done
pkill -f '[c]loud-benchmark.sh __exec' 2>/dev/null || true
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t $TMUX_SESSION 2>/dev/null
  tmux new-session -d -s $TMUX_SESSION './start.sh'
  echo 'LAUNCH=tmux 会话=$TMUX_SESSION'
else
  setsid nohup ./start.sh >/dev/null 2>&1 </dev/null &
  echo \"LAUNCH=nohup pid=\$!\"
fi"
  rsh "$target" "$launch" || die "后台启动失败"

  c_ok "已在远端后台执行（tmux 或 setsid+nohup 回退，SSH 断连不中断）"
  c_info "运行 ID: $run_id"
  c_info "查看进度: $0 status --name $name"
  c_info "回收结果: $0 collect --name $name"
}

show_remote_run_status() {
  local target="$1" remote_out="$2"
  rsh "$target" "
    echo '--- 运行状态 ---'
    if [ -f $remote_out/run-status.tsv ]; then
      cat $remote_out/run-status.tsv
    else
      echo '（尚未写入 run-status.tsv：可能正在启动/定时等待，或旧版目录）'
    fi
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t $TMUX_SESSION 2>/dev/null; then
      echo 'tmux 会话运行中: $TMUX_SESSION'
    elif [ -f $remote_out/exec.pid ] && kill -0 \$(cat $remote_out/exec.pid) 2>/dev/null; then
      echo \"后台进程运行中 (PID \$(cat $remote_out/exec.pid))\"
    else
      echo '（无运行中的任务）'
    fi
    echo '--- 阶段状态 ---'
    cat $remote_out/stage-status.tsv 2>/dev/null || echo '（无 stage-status.tsv）'
    echo '--- 阶段进度 ---'
    tail -12 $remote_out/progress.log 2>/dev/null || echo '（无 progress.log）'
    echo '--- 已产出文件 ---'
    ls -la $remote_out 2>/dev/null | tail -20
    echo '--- 驱动器日志尾部 ---'
    tail -5 $remote_out/driver.log 2>/dev/null
  "
}

cmd_status() {
  local name="" run_id="" batch_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name)  name="$2"; shift 2 ;;
      --run)   run_id="$2"; shift 2 ;;
      --batch)
        [ $# -ge 2 ] || die "--batch 需要批次 ID"
        batch_id="$2"; shift 2 ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$name" ] || die "用法: $0 status --name <主机名> [--run ID | --batch ID]"
  require_run_id "$run_id"
  require_batch_id "$batch_id"
  [ -z "$run_id" ] || [ -z "$batch_id" ] || die "--run 与 --batch 不能同时使用"
  local target; target="$(host_target "$name")"

  if [ -n "$batch_id" ]; then
    require_batch_id "$batch_id"
    local manifest
    manifest=$(rsh "$target" "cat $REMOTE_BATCHES/$batch_id/batch.tsv 2>/dev/null") \
      || die "无法读取批次清单（SSH 失败）"
    [ -n "$manifest" ] || die "批次不存在或清单为空: $batch_id"
    local run_ids; run_ids="$(batch_field "$manifest" run_ids)"
    [ -n "$run_ids" ] || die "批次清单缺少 run_ids: $batch_id"

    c_head "$name ($target) 批次进度 · $batch_id"
    echo "$manifest" | sed 's/^/  /'
    echo '--- 批次驱动日志 ---'
    rsh "$target" "tail -8 $REMOTE_BATCHES/$batch_id/driver.log 2>/dev/null" || true
    local rid status
    for rid in $run_ids; do
      require_run_id "$rid"
      status=$(rsh "$target" "grep '^status' $REMOTE_RUNS/$rid/out/run-status.tsv 2>/dev/null | cut -f2" | tr -d '\r')
      if [ -z "$status" ]; then
        if rsh "$target" "test -d $REMOTE_RUNS/$rid/out"; then status=starting; else status=pending; fi
      fi
      echo
      c_info "===== $rid · $status ====="
      show_remote_run_status "$target" "$REMOTE_RUNS/$rid/out"
    done
    return 0
  fi

  [ -n "$run_id" ] || run_id="$(latest_run_id "$target")"
  if [ -n "$run_id" ] && ! valid_run_id "$run_id"; then
    die "远端 latest-run 内容非法: $run_id"
  fi
  local remote_out
  if [ -n "$run_id" ]; then
    remote_out="$REMOTE_RUNS/$run_id/out"
  else
    remote_out="$REMOTE_OUT"
  fi

  c_head "$name ($target) 进度${run_id:+ · $run_id}"
  show_remote_run_status "$target" "$remote_out"
}

# 回收单次 run；batch 与单 run 共用，保证状态保护、原子落盘、PTS 备份只有一份实现。
# run_id 为空表示旧版扁平目录 results/<name>/。
collect_one_run() {
  local name="$1" target="$2" run_id="$3" force="$4"
  local remote_out dest
  if [ -n "$run_id" ]; then
    remote_out="$REMOTE_RUNS/$run_id/out"
    dest="$RESULTS_DIR/$name/$run_id"
  else
    remote_out="$REMOTE_OUT"
    dest="$RESULTS_DIR/$name"
  fi

  # 运行中/尚未进入执行阶段时默认不回收；--force 才允许拿半份数据。
  # 新 run 在 __exec 写 run-status 之前没有状态文件，必须视为 starting，
  # 否则 run 刚启动就 collect 会收走一个空目录。
  local rstatus
  rstatus=$(rsh "$target" "grep '^status' $remote_out/run-status.tsv 2>/dev/null | cut -f2" | tr -d '\r')
  if [ -n "$run_id" ] && [ -z "$rstatus" ]; then
    rstatus=starting
  fi
  case "$rstatus" in
    running|starting)
      [ -n "$force" ] || die "运行尚未完成（${run_id:-legacy}，状态=$rstatus）；等结束或加 --force 强制回收"
      ;;
  esac

  local rhost; rhost=$(rsh "$target" 'hostname') || die "无法连接 $target（SSH 失败）"
  mkdir -p "$RESULTS_DIR/$name"
  c_head "回收 $name ($target, hostname=$rhost)${run_id:+ · $run_id} → $dest"

  # 目标已存在时不静默覆盖；--force 会保留一份 .bak 备份。
  if [ -d "$dest" ] && [ -z "$(ls -A "$dest" 2>/dev/null)" ]; then
    rmdir "$dest" 2>/dev/null || true
  fi
  if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
    if [ -n "$force" ]; then
      mv "$dest" "$dest.bak.$(date +%Y%m%d%H%M%S)" || die "备份旧结果失败"
    else
      die "目标已存在：$dest；确认覆盖请加 --force（会保留备份）"
    fi
  fi

  # 用 tar over ssh 拉取（不依赖远端 rsync），先落临时目录，成功后再原子改名。
  local tmp
  tmp=$(mktemp -d "${dest}.tmp.XXXXXX") || die "创建临时目录失败: ${dest}.tmp.XXXXXX"
  if ! rsh "$target" "cd $remote_out && tar czf - \
      --exclude='./fio' --exclude='*.tgz' --exclude='yabs.sh' \
      --exclude='20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*' ." \
      | tar xzf - -C "$tmp"; then
    rm -r "$tmp" 2>/dev/null
    die "结果拉取失败"
  fi
  mv "$tmp" "$dest" || die "写入结果目录失败"

  # LATEST 表示「远端最近一次 run」，不是「最后一次成功 collect 的 run」。
  # 显式回收旧 run 时不能把本地 LATEST 倒退回旧值。
  if [ -n "$run_id" ]; then
    local remote_latest
    remote_latest=$(latest_run_id "$target")
    if [ -n "$remote_latest" ] && valid_run_id "$remote_latest" && \
       [ "$run_id" = "$remote_latest" ]; then
      printf '%s\n' "$run_id" > "$RESULTS_DIR/$name/LATEST"
    else
      c_info "保留本地 LATEST（远端最新=${remote_latest:-未知}，本次回收=$run_id）"
    fi
  fi

  # 同时备份 PTS 的 test-results：composite.xml 是权威结果源，不会随 pts.log 覆盖。
  # 新产物优先读 pts-result-names.txt（可能多个）；其次兼容旧的单值文件。
  local pts_names="" pts_name
  pts_names=$(rsh "$target" "cat $remote_out/pts-result-names.txt 2>/dev/null" | tr -d '\r')
  if [ -z "$pts_names" ]; then
    pts_name=$(rsh "$target" "cat $remote_out/pts-result-name.txt 2>/dev/null" | tr -d '\r\n')
    [ -n "$pts_name" ] && pts_names="$pts_name"
  fi
  # 新 run 如果没有本次结果标记，就不要猜「最新目录」——那可能是另一轮 run 的结果。
  # 只有旧版扁平目录（没有 run_id）才回退到 latest。
  if [ -z "$pts_names" ] && [ -z "$run_id" ]; then
    local pts_res
    pts_res=$(rsh "$target" "ls -td ~/.phoronix-test-suite/test-results/*/ 2>/dev/null | head -1")
    [ -n "$pts_res" ] && pts_names=$(basename "$pts_res")
  fi
  for pts_name in $pts_names; do
    if ! printf '%s' "$pts_name" | grep -Eq '^[A-Za-z0-9._-]+$'; then
      c_warn "跳过非法 PTS 结果目录名: $pts_name"
      continue
    fi
    mkdir -p "$dest/pts-$pts_name"
    rsh "$target" "cd ~/.phoronix-test-suite/test-results/$pts_name && tar czf - composite.xml test-logs 2>/dev/null" \
      | tar xzf - -C "$dest/pts-$pts_name" 2>/dev/null \
      && c_info "已备份 PTS 结果: $pts_name（composite.xml + test-logs）"
  done

  {
    echo "name=$name"
    echo "run_id=$run_id"
    echo "run_status=$rstatus"
    echo "ssh_target=$target"
    echo "remote_hostname=$rhost"
    echo "collected_at=$(date -Is)"
  } > "$dest/meta.env"

  c_ok "已回收：$(ls -1 "$dest" | tr '\n' ' ')"
}

cmd_collect() {
  local name="" run_id="" batch_id="" force=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name)  name="$2"; shift 2 ;;
      --run)   run_id="$2"; shift 2 ;;
      --batch)
        [ $# -ge 2 ] || die "--batch 需要批次 ID"
        batch_id="$2"; shift 2 ;;
      --force) force=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$name" ] || die "用法: $0 collect --name <主机名> [--run ID | --batch ID] [--force]"
  require_run_id "$run_id"
  require_batch_id "$batch_id"
  [ -z "$run_id" ] || [ -z "$batch_id" ] || die "--run 与 --batch 不能同时使用"
  local target; target="$(host_target "$name")"

  if [ -n "$batch_id" ]; then
    require_batch_id "$batch_id"
    local manifest
    manifest=$(rsh "$target" "cat $REMOTE_BATCHES/$batch_id/batch.tsv 2>/dev/null") \
      || die "无法读取批次清单（SSH 失败）"
    [ -n "$manifest" ] || die "批次不存在或清单为空: $batch_id"
    local run_ids repeat count=0
    run_ids="$(batch_field "$manifest" run_ids)"
    repeat="$(batch_field "$manifest" repeat)"
    [ -n "$run_ids" ] || die "批次清单缺少 run_ids: $batch_id"

    # 先全量预检：任何 run 还在运行、目标非空或远端目录缺失，都在拉取前失败。
    local busy="" conflicts="" missing="" rid status dest
    for rid in $run_ids; do
      require_run_id "$rid"
      count=$((count + 1))
      if ! rsh "$target" "test -d $REMOTE_RUNS/$rid/out"; then
        missing="$missing $rid"
        continue
      fi
      status=$(rsh "$target" "grep '^status' $REMOTE_RUNS/$rid/out/run-status.tsv 2>/dev/null | cut -f2" | tr -d '\r')
      [ -n "$status" ] || status=starting
      case "$status" in
        running|starting) busy="$busy $rid($status)" ;;
      esac
      dest="$RESULTS_DIR/$name/$rid"
      if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        conflicts="$conflicts $rid"
      fi
    done
    [ -n "$repeat" ] || repeat="$count"
    [ "$repeat" = "$count" ] || die "批次清单 repeat=$repeat 与 run_ids 数量=$count 不一致"
    [ -z "$missing" ] || die "批次内远端 run 目录缺失:$missing"
    [ -n "$force" ] || [ -z "$busy" ] || die "批次内仍有未完成的 run:$busy；等结束或加 --force"
    [ -n "$force" ] || [ -z "$conflicts" ] || die "批次内本地目标已存在:$conflicts；确认覆盖请加 --force（会保留备份）"

    c_head "回收批次 $name ($target) · $batch_id（$count 次 run）"
    for rid in $run_ids; do
      collect_one_run "$name" "$target" "$rid" "$force"
    done

    mkdir -p "$RESULTS_DIR/$name/batches"
    local tmp_manifest="$RESULTS_DIR/$name/batches/.${batch_id}.tsv.tmp"
    printf '%s\n' "$manifest" > "$tmp_manifest" || die "写入批次清单失败"
    mv "$tmp_manifest" "$RESULTS_DIR/$name/batches/$batch_id.tsv" || die "保存批次清单失败"
    c_ok "批次已回收：$batch_id（清单 results/$name/batches/$batch_id.tsv）"
    return 0
  fi

  [ -n "$run_id" ] || run_id="$(latest_run_id "$target")"
  if [ -n "$run_id" ] && ! valid_run_id "$run_id"; then
    die "远端 latest-run 内容非法: $run_id"
  fi
  collect_one_run "$name" "$target" "$run_id" "$force"
}

iperf_start_server() {
  local target="$1"
  rsh "$target" "
    pkill -x iperf3 2>/dev/null
    sleep 1
    if command -v tmux >/dev/null 2>&1; then
      tmux kill-session -t iperf3srv 2>/dev/null
      tmux new-session -d -s iperf3srv 'iperf3 -s -1 -p 5201 > /tmp/iperf3-server.log 2>&1'
    else
      setsid nohup iperf3 -s -1 -p 5201 > /tmp/iperf3-server.log 2>&1 </dev/null &
    fi
    sleep 1
    echo server_ready" 2>&1 | tail -1
}

# 跨主机 iperf3 互测（双向 -P 8）：A 作 server，B 作 client，再反向
cmd_iperf() {
  local a="" b=""
  while [ $# -gt 0 ]; do
    case "$1" in --a) a="$2"; shift 2 ;; --b) b="$2"; shift 2 ;; *) die "未知参数: $1" ;; esac
  done
  [ -n "$a" ] && [ -n "$b" ] || die "用法: $0 iperf --a <主机A> --b <主机B>"

  local ta tb ipa ipb
  ta="$(host_target "$a")"; tb="$(host_target "$b")"
  # 用私网 IP 还是公网 IP：优先公网（轻量套餐限速作用于公网），这里取公网
  ipa=$(host_field "$a" 3); ipb=$(host_field "$b" 3)

  c_head "iperf3 互测: $a ($ipa) <-> $b ($ipb)"

  for dir in "A->B" "B->A"; do
    # ⚠️ 两个实测 bug：
    #   ① dst 必须用 **ssh 别名**（host_target 的结果），不能直接塞主机清单名——
    #      清单名（如 volc-4c8g-01）不是 ssh 能解析的目标，rsh 会直接失败；
    #   ② server 必须先在**目标端**起来再跑 client。原先在循环外先给 A 起 server，
    #      导致第 1 轮 A→B 时 B 上根本没有 server，只能连出超时。
    local src dst dstip label
    if [ "$dir" = "A->B" ]; then src="$ta"; dst="$tb"; dstip="$ipb"; label="${a}__to__${b}"
    else src="$tb"; dst="$ta"; dstip="$ipa"; label="${b}__to__${a}"; fi
    c_info "方向 $dir ($label)"
    iperf_start_server "$dst"
    rsh "$src" "iperf3 -c $dstip -p 5201 -P 8 -t 15 --json" > "$RESULTS_DIR/iperf3-${label}.json" 2>&1
    local mbps
    mbps=$(jq -r '.end.sum_sent.bits_per_second / 1000000 // empty' "$RESULTS_DIR/iperf3-${label}.json" 2>/dev/null)
    if [ -n "$mbps" ]; then c_ok "  $label: ${mbps} Mbps"; else c_warn "  $label: 未取到结果（1M 带宽机器可能极慢）"; fi
  done
  # 清理两端的 server 会话/进程
  for t in "$ta" "$tb"; do
    rsh "$t" "tmux kill-session -t iperf3srv 2>/dev/null; pkill -x iperf3 2>/dev/null; true"
  done
  c_ok "iperf3 互测完成，JSON 见 results/iperf3-*.json"
}

cmd_summarize() {
  local batch_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --batch)
        [ $# -ge 2 ] || die "--batch 需要批次 ID"
        batch_id="$2"; shift 2 ;;
      *) die "未知参数: $1" ;;
    esac
  done
  require_batch_id "$batch_id"
  c_head "汇总 results/ 下所有主机${batch_id:+ · 批次 $batch_id}"
  local -a batch_args=()
  [ -n "$batch_id" ] && batch_args=(--batch "$batch_id")
  python3 "$REPO_ROOT/scripts/cloud-benchmark-summarize.py" ${batch_args[@]+"${batch_args[@]}"} \
    || die "汇总脚本执行失败"
}

# 提交评测结果到站点（先本地生成 JSON，再 POST）
# 面向初级用户的关键设计：默认先 dry-run 给用户看一眼要上传什么，确认后再提交。
cmd_submit() {
  local name="" api="" key="" dry="" run_id="" batch_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name)    name="$2"; shift 2 ;;
      --api)     api="$2"; shift 2 ;;
      --key)     key="$2"; shift 2 ;;
      --run)     run_id="$2"; shift 2 ;;
      --batch)
        [ $# -ge 2 ] || die "--batch 需要批次 ID"
        batch_id="$2"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$name" ] || die "用法: $0 submit --name <主机名> --api <站点URL> [--run ID | --batch ID] [--dry-run]"
  require_run_id "$run_id"
  require_batch_id "$batch_id"
  [ -z "$run_id" ] || [ -z "$batch_id" ] || die "--run 与 --batch 不能同时使用"
  local -a run_args=()
  [ -n "$run_id" ] && run_args=(--run "$run_id")
  [ -n "$batch_id" ] && run_args=(--batch "$batch_id")
  api="${api:-${BENCH_API_URL:-}}"

  # 本地生成（同时把「哪些指标没测到」提示出来，避免把一堆 0 值传上去还不自知）
  if [ -n "$dry" ]; then
    python3 "$REPO_ROOT/scripts/to-result.py" --name "$name" --results "$RESULTS_DIR" \
      ${run_args[@]+"${run_args[@]}"} || die "生成结果 JSON 失败"
    c_info "以上是即将提交的内容（--dry-run，未上传）"
    return 0
  fi

  [ -n "$api" ] || die "缺少站点地址：--api <URL> 或 export BENCH_API_URL=..."
  # 密钥优先取环境变量：写成 --key 参数会留在 shell 历史里
  local k="${key:-${BENCH_API_KEY:-}}"
  [ -n "$k" ] || die "缺少提交密钥：推荐 export BENCH_API_KEY=xxx；也可用 --key（注意会留在命令历史）"

  c_head "提交 $name 的评测结果 → $api"
  BENCH_API_KEY="$k" python3 "$REPO_ROOT/scripts/to-result.py" \
    --name "$name" --results "$RESULTS_DIR" ${run_args[@]+"${run_args[@]}"} --submit "$api" \
    || die "提交失败（见上方错误信息）"
  c_ok "已提交"
}

# 单机「人话报告」——面向初级用户：把指标翻译成「会不会卡 / 是不是虚标 / 能跑什么」
cmd_report() {
  local name="" run_id="" batch_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name)  name="$2"; shift 2 ;;
      --run)   run_id="$2"; shift 2 ;;
      --batch)
        [ $# -ge 2 ] || die "--batch 需要批次 ID"
        batch_id="$2"; shift 2 ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$name" ] || die "用法: $0 report --name <主机名> [--run ID | --batch ID]"
  require_run_id "$run_id"
  require_batch_id "$batch_id"
  [ -z "$run_id" ] || [ -z "$batch_id" ] || die "--run 与 --batch 不能同时使用"
  local -a run_args=()
  [ -n "$run_id" ] && run_args=(--run "$run_id")
  [ -n "$batch_id" ] && run_args=(--batch "$batch_id")
  python3 "$REPO_ROOT/scripts/cloud-benchmark-summarize.py" --report "$name" ${run_args[@]+"${run_args[@]}"} \
    || die "报告生成失败（该主机有产物吗？先 run + collect）"
}

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
main() {
  local cmd="${1:-}"
  [ $# -gt 0 ] && shift
  case "$cmd" in
    run)       cmd_run "$@" ;;
    report)    cmd_report "$@" ;;
    submit)    cmd_submit "$@" ;;
    status)    cmd_status "$@" ;;
    collect)   cmd_collect "$@" ;;
    iperf)     cmd_iperf "$@" ;;
    summarize) cmd_summarize "$@" ;;
    __exec)    # 远端内部入口
      local out="" stages="$ALL_STAGES"
      while [ $# -gt 0 ]; do
        case "$1" in --out) out="$2"; shift 2 ;; --stages) stages="$(echo "$2" | tr ',' ' ')"; shift 2 ;; *) shift ;; esac
      done
      remote_exec "$out" "$stages" ;;
    -h|--help|help|"") usage ;;
    *) die "未知命令: $cmd（用 --help 看用法）" ;;
  esac
}

main "$@"
