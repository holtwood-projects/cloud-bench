#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud-bench 共享解析层。

这里是所有评测产物的唯一权威解析入口：
    fio JSON / sysbench 日志 / mpstat 日志 / YABS JSON / Speedtest / PTS 日志

`cloud-benchmark-summarize.py`（报告/对比表）和 `to-result.py`（提交）
都从本模块取数，避免两边对同一份原始产物给出不同结果。

设计约定：
  - 原始产物优先；`metrics.tsv` 只作为旧版产物的兜底缓存。
  - 解析函数接收单次运行的产物目录 `d`，不依赖全局 RESULTS。
  - 运行目录可以是旧版扁平目录，也可以是 `results/<name>/<run_id>/`。
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from collections import OrderedDict

SCHEMA_VERSION = 2
TOOL_VERSION = "1.2.0"

# 展示用列定义：(列名, 指标键, 小数位)
METRIC_COLS = [
    ("单核(events/s)",   "sysbench_cpu_single_eps", 1),
    ("多核(events/s)",   "sysbench_cpu_multi_eps", 1),
    ("4K读IOPS",         "fio_4k_randread_iops", 0),
    ("4K写IOPS",         "fio_4k_randwrite_iops", 0),
    ("4K读p99(ms)",      "fio_4k_randread_p99_ms", 2),
    ("4K读p99.9(ms)",    "fio_4k_randread_p999_ms", 2),
    ("4K写p99(ms)",      "fio_4k_randwrite_p99_ms", 2),
    ("顺序读(MiB/s)",    "fio_seq_read_mib_s", 1),
    ("顺序写(MiB/s)",    "fio_seq_write_mib_s", 1),
    ("内存带宽(MiB/s)",  "sysbench_mem_single_mib_s", 0),
    ("下行(Mbps)",       "speedtest_down_mbps", 2),
    ("上行(Mbps)",       "speedtest_up_mbps", 2),
    ("ping(ms)",         "speedtest_latency_ms", 1),
    ("满载%steal(均)",   "stress_avg_steal_pct", 2),
    ("满载%steal(峰)",   "stress_max_steal_pct", 2),
    ("YABS4K总IOPS",     "yabs_4k_total_iops", 0),
    ("7z压缩(MIPS)",     "pts_7zip_compress_mips", 0),
    ("redis SET(RPS)",   "pts_redis_rps", 0),
]

# 零值即缺失的指标：实测必然 > 0，0 只可能是「未跑 / 跑失败 / 解析失败」。
# 注意 steal_avg_pct 刻意不在列——满载 0% 抢占是合法且有意义的结果。
ZERO_MEANS_MISSING = (
    "cpu_single_eps", "cpu_multi_eps",
    "sevenzip_compress_mips", "sevenzip_decompress_mips", "kernel_build_sec",
    "disk_4k_read_iops", "disk_4k_write_iops",
    "disk_4k_read_p99_ms", "disk_4k_read_p999_ms",
    "disk_4k_write_p99_ms", "disk_4k_write_p999_ms",
    "disk_seq_read_mibs", "disk_seq_write_mibs",
    "mem_bandwidth_mibs",
    "net_down_mbps", "net_up_mbps", "net_ping_ms",
    "redis_rps", "yabs_4k_total_iops",
)

LEGACY_STAGE_FILES = [
    ("env", "system-info.txt"), ("yabs", "yabs.log"), ("pts", "pts.log"),
    ("fio", "fio.log"), ("sysbench", "sysbench.log"),
    ("stress", "stress-ng.log"), ("net", "speedtest.log"),
]


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def safe_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 原始产物解析
# ---------------------------------------------------------------------------
def _fio_job_metrics(path, rw, prefix):
    """解析 fio JSON：IOPS / 带宽 / p99 / p99.9（ns → ms）。"""
    j = load_json(path)
    if not j or not j.get("jobs"):
        return {}
    job = j["jobs"][0]
    sec = job.get(rw) or {}
    out = {}
    if sec.get("iops"):
        out[f"{prefix}_iops"] = sec["iops"]
    if sec.get("bw"):
        out[f"{prefix}_bw"] = sec["bw"] / 1024.0          # KiB/s → MiB/s
    pct = (sec.get("clat_ns") or {}).get("percentile") or {}
    for key, value in pct.items():
        try:
            point = float(key)
        except ValueError:
            continue
        try:
            if abs(point - 99.0) < 1e-6:
                out[f"{prefix}_p99_ms"] = value / 1e6     # ns → ms
            elif abs(point - 99.9) < 1e-6:
                out[f"{prefix}_p999_ms"] = value / 1e6
        except TypeError:
            continue
    return out


def parse_fio(d):
    """fio 精测：4K 随机读/写（含 p99/p99.9）+ 顺序读/写。"""
    m = {}
    m.update(_fio_job_metrics(os.path.join(d, "fio-4k-randread.json"), "read", "fio_4k_randread"))
    m.update(_fio_job_metrics(os.path.join(d, "fio-4k-randwrite.json"), "write", "fio_4k_randwrite"))
    for rw, key in (("read", "fio_seq_read_mib_s"), ("write", "fio_seq_write_mib_s")):
        j = load_json(os.path.join(d, f"fio-seq-{rw}.json"))
        if j and j.get("jobs"):
            bw = (j["jobs"][0].get(rw) or {}).get("bw")
            if bw:
                m[key] = bw / 1024.0
    return m


def parse_sysbench(d):
    """sysbench：CPU 单核/全核 events/s，内存带宽 MiB/s。

    必须按测试段解析：sysbench memory 也输出 events per second，
    全局 findall 会在 CPU 某段失败时把内存结果错位成 CPU 成绩。
    """
    txt = read_text(os.path.join(d, "sysbench.log"))
    if not txt:
        return {}
    m = {}

    def seg(start_marker, end_marker=None):
        i = txt.find(start_marker)
        if i < 0:
            return ""
        j = txt.find(end_marker, i) if end_marker else len(txt)
        return txt[i:j]

    cpu1 = seg("--- CPU 单线程 60s ---", "--- CPU 全核")
    cpuN = seg("--- CPU 全核", "--- 内存带宽")
    mem1 = seg("--- 内存带宽（1M 块，单线程，60s）---", "--- 内存带宽（1M 块，全核")
    memN = seg("--- 内存带宽（1M 块，全核")
    for blob, key in ((cpu1, "sysbench_cpu_single_eps"),
                      (cpuN, "sysbench_cpu_multi_eps")):
        v = re.search(r"events per second:\s*([0-9.]+)", blob)
        if v:
            m[key] = float(v.group(1))
    for blob, key in ((mem1, "sysbench_mem_single_mib_s"),
                      (memN, "sysbench_mem_multi_mib_s")):
        v = re.search(r"([0-9.]+)\s*MiB/sec", blob)
        if v:
            m[key] = float(v.group(1))
    return m


def parse_stress(d):
    """超售检测：mpstat 的 %steal（均值/峰值）与 %idle 均值。"""
    txt = read_text(os.path.join(d, "mpstat.log"))
    if not txt:
        return {}
    steal_idx = idle_idx = None
    steals, idles = [], []
    for line in txt.splitlines():
        parts = line.split()
        if "%steal" in parts:
            steal_idx = parts.index("%steal")
            idle_idx = parts.index("%idle") if "%idle" in parts else None
            continue
        if steal_idx is None or line.startswith(("Linux", "Average:")):
            continue
        if len(parts) <= steal_idx:
            continue
        try:
            steals.append(float(parts[steal_idx]))
            if idle_idx is not None and len(parts) > idle_idx:
                idles.append(float(parts[idle_idx]))
        except (ValueError, IndexError):
            continue
    m = {}
    if steals:
        m["stress_avg_steal_pct"] = sum(steals) / len(steals)
        m["stress_max_steal_pct"] = max(steals)
        m["stress_samples"] = len(steals)
    if idles:
        m["stress_avg_idle_pct"] = sum(idles) / len(idles)
    return m


def parse_yabs(d):
    """YABS：4K 混合 fio（交叉印证）、Geekbench 结果链接、运行时长。"""
    j = None
    for cand in ("yabs-result.json", "yabs_result.json", "result.json"):
        j = load_json(os.path.join(d, cand))
        if j:
            break
    if not j:
        return {}
    m = {}
    for entry in (j.get("fio") or []):
        if entry.get("bs") == "4k":
            if entry.get("iops_r"):
                m["yabs_4k_read_iops"] = entry["iops_r"]
            if entry.get("iops_w"):
                m["yabs_4k_write_iops"] = entry["iops_w"]
            if entry.get("iops_rw"):
                m["yabs_4k_total_iops"] = entry["iops_rw"]
            break
    gb = (j.get("geekbench") or [{}])[0]
    if gb.get("url"):
        m["yabs_geekbench_url"] = gb["url"]
    if gb.get("single"):
        m["yabs_geekbench_single"] = gb["single"]
    if gb.get("multi"):
        m["yabs_geekbench_multi"] = gb["multi"]
    if (j.get("runtime") or {}).get("elapsed"):
        m["yabs_elapsed_sec"] = j["runtime"]["elapsed"]
    return m


def parse_speedtest(d):
    """Ookla JSON（可能被 EULA 文本包住）或 speedtest-cli --simple 文本。"""
    txt = read_text(os.path.join(d, "speedtest.json")) or read_text(os.path.join(d, "speedtest.log"))
    if not txt:
        txt = read_text(os.path.join(d, "speedtest.txt"))
    if not txt:
        return {}
    m = {}
    line = None
    for cand in reversed(txt.splitlines()):
        if '"type":"result"' in cand:
            line = cand
            break
    if line:
        try:
            res = json.loads(line)
        except ValueError:
            res = None
        if res:
            dl = (res.get("download") or {}).get("bandwidth")
            ul = (res.get("upload") or {}).get("bandwidth")
            ping = res.get("ping") or {}
            if dl:
                m["speedtest_down_mbps"] = dl / 125000.0
            if ul:
                m["speedtest_up_mbps"] = ul / 125000.0
            if ping.get("latency") is not None:
                m["speedtest_latency_ms"] = ping["latency"]
            if ping.get("jitter") is not None:
                m["speedtest_jitter_ms"] = ping["jitter"]
            if (res.get("server") or {}).get("name"):
                m["speedtest_server"] = res["server"]["name"]
            return m

    # 旧版/离线回退：speedtest-cli --simple
    for key, pattern in (
        ("speedtest_down_mbps", r"^Download:\s*([0-9.]+)\s*Mbit/s"),
        ("speedtest_up_mbps", r"^Upload:\s*([0-9.]+)\s*Mbit/s"),
        ("speedtest_latency_ms", r"^Ping:\s*([0-9.]+)\s*ms"),
    ):
        mm = re.search(pattern, txt, re.M)
        if mm:
            m[key] = float(mm.group(1))
    return m


def parse_pts(d):
    """PTS：7-Zip、Redis，以及内核编译秒数（有明确 label 时）。"""
    txt = read_text(os.path.join(d, "pts.log"))
    if not txt:
        return {}
    # PTS 在 pty 下会输出 ANSI 颜色码，夹在 Average: 与数字之间
    txt = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", txt)
    m = {}
    for label, key in (("Compression Rating", "pts_7zip_compress_mips"),
                       ("Decompression Rating", "pts_7zip_decompress_mips")):
        start = txt.find(label + ":")
        if start >= 0:
            seg = txt[start:start + 3000]
            avg = re.search(r"Average:\s*([0-9.]+)\s*MIPS", seg)
            if avg:
                m[key] = float(avg.group(1))
    start = txt.find("SET - Parallel Connections: 50:")
    if start >= 0:
        seg = txt[start:start + 3000]
        avg = re.search(r"Average:\s*([0-9.,]+)\s*Requests Per Second", seg)
        if avg:
            m["pts_redis_rps"] = float(avg.group(1).replace(",", ""))

    # 内核编译只认 label 附近的 Average Seconds，避免误取其它测试的秒数
    for label in ("build-linux-kernel", "Build Linux Kernel", "Linux Kernel"):
        start = txt.find(label)
        if start >= 0:
            seg = txt[start:start + 20000]
            avg = re.search(r"Average:\s*([0-9.]+)\s*Seconds", seg)
            if avg:
                m["pts_kernel_build_sec"] = float(avg.group(1))
                break

    if "batch mode must first be configured" in txt:
        m["_pts_batch_unconfigured"] = True
    if re.search(r"^\s*Estimated Download Time", txt, re.M) and \
       not re.search(r"Average:\s*[0-9.]+", txt):
        m["_pts_only_downloaded"] = True
    return m


def parse_remote_metrics(d):
    """旧版 metrics.tsv（只作为兜底；原始产物优先）。"""
    m = {}
    path = os.path.join(d, "metrics.tsv")
    if not os.path.isfile(path):
        return m
    for line in read_text(path).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] not in ("", "null"):
            try:
                m[parts[0]] = float(parts[1])
            except ValueError:
                m[parts[0]] = parts[1]
    return m


def collect_metrics(d):
    """原始产物优先，metrics.tsv 补缺。返回 (merged, local, remote)。"""
    local = {}
    for fn in (parse_fio, parse_sysbench, parse_stress, parse_yabs, parse_speedtest, parse_pts):
        try:
            local.update({k: v for k, v in fn(d).items() if v is not None})
        except Exception as exc:                          # 单文件异常不影响整体
            print(f"  [warn] {d} {fn.__name__} 解析异常: {exc}", file=sys.stderr)
    remote = parse_remote_metrics(d)
    merged = dict(remote)
    merged.update(local)
    # 旧 metrics.tsv 里只有 _us，统一补一份 _ms，供展示层直接读取。
    for prefix in ("fio_4k_randread", "fio_4k_randwrite"):
        for pct in ("p99", "p999"):
            us_key = f"{prefix}_{pct}_us"
            ms_key = f"{prefix}_{pct}_ms"
            if us_key in merged and ms_key not in merged:
                try:
                    merged[ms_key] = float(merged[us_key]) / 1000.0
                except (TypeError, ValueError):
                    pass
    return merged, local, remote


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def _tail_int(line):
    try:
        return int(line.split(":")[-1].strip())
    except ValueError:
        return None


def read_sysinfo(d):
    """从 system-info.txt 提取 CPU 型号 / 物理核 / 线程 / 指令集 / 内核等。"""
    info = {
        "cpu_model": "", "physical_cores": 0, "threads": 0,
        "avx512": False, "kernel": "", "virtualization": "",
        "governor": "", "scheduler": "", "loadavg": "", "container_count": 0,
    }
    txt = read_text(os.path.join(d, "system-info.txt"))
    if not txt:
        return info
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Model name:"):
            info["cpu_model"] = s.split(":", 1)[1].strip()
        elif s.startswith("CPU(s):"):
            info["threads"] = safe_int(s.split(":", 1)[1], 0)
        elif "Thread(s) per core" in line:
            info["_tpc"] = _tail_int(line)
        elif "Core(s) per socket" in line:
            info["_cps"] = _tail_int(line)
        elif "Socket(s)" in line:
            info["_socks"] = _tail_int(line)
        elif s.lower().startswith("kernel:"):
            info["kernel"] = s.split(":", 1)[1].strip()
        elif s.startswith("虚拟化类型:"):
            info["virtualization"] = s.split(":", 1)[1].strip()
        elif s.startswith("cpufreq governor:"):
            info["governor"] = s.split(":", 1)[1].strip()
        elif s.startswith("loadavg:"):
            info["loadavg"] = s.split(":", 1)[1].strip()
        elif s.startswith("物理核数(去重 Core,Socket)="):
            info["physical_cores"] = safe_int(s.split("=", 1)[1], 0)
        elif re.match(r"^\d{1,3}(\.\d+)?\s+\d{1,3}(\.\d+)?\s+\d{1,3}(\.\d+)?$", s):
            info["loadavg"] = s
        elif "/queue/scheduler:" in s:
            info["scheduler"] = s.split(":", 1)[-1].strip()
    if not info["physical_cores"] and all(info.get(k) for k in ("_tpc", "_cps", "_socks")):
        info["physical_cores"] = info["_cps"] * info["_socks"]

    # stage_env 会输出「avx512f 有/无」；优先按这一行判定。
    # 旧产物没有该行时，才从 flags 行里找，避免把「avx512f 无」误判成有。
    avx_line = re.search(r"^\s*avx512f\s+(有|无)\s*$", txt, re.M)
    if avx_line:
        info["avx512"] = avx_line.group(1) == "有"
    else:
        info["avx512"] = any(
            "avx512f" in line.split()
            for line in txt.splitlines()
            if not line.strip().startswith("avx512f")
        )

    # 容器数：stage_env 的 docker ps 输出每条一行，表头/提示行不算
    docker_lines = 0
    in_docker = False
    for line in txt.splitlines():
        if line.strip() == "--- 容器（如有）---":
            in_docker = True
            continue
        if in_docker:
            if line.startswith("##########"):
                break
            if "\t" in line and "（无 docker" not in line:
                docker_lines += 1
    info["container_count"] = docker_lines
    return info


# ---------------------------------------------------------------------------
# hosts.tsv / 运行目录 / 状态
# ---------------------------------------------------------------------------
def read_hosts(hosts_file):
    """读取 hosts.tsv，返回 OrderedDict 列表；region/vendor_type 为可选列。"""
    host_list = []
    for line in read_text(hosts_file).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        p = line.split("\t")
        if len(p) < 12:
            continue
        if p[0].strip() == "name" and p[1].strip() == "ssh_alias":
            continue
        host_list.append(OrderedDict([
            ("name", p[0].strip()), ("ssh_alias", p[1].strip()), ("ip", p[2].strip()),
            ("user", p[3].strip()), ("port", p[4].strip()), ("vendor", p[5].strip()),
            ("spec", p[6].strip()), ("vcpu", p[7].strip()), ("ram_gb", p[8].strip()),
            ("bandwidth", p[9].strip()), ("price_cny", p[10].strip()),
            ("price_note", p[11].strip()),
            ("role", p[12].strip() if len(p) > 12 else ""),
            ("region", p[13].strip() if len(p) > 13 else ""),
            ("vendor_type", p[14].strip() if len(p) > 14 else ""),
        ]))
    return host_list


def hosts_by_name(hosts_file):
    return {h["name"]: h for h in read_hosts(hosts_file)}


def read_run_status(d):
    """读取 run-status.tsv（key<TAB>value）。旧版目录返回空字典。"""
    status = {}
    for line in read_text(os.path.join(d, "run-status.tsv")).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[0].strip()] = parts[1].strip()
    return status


def read_stage_status(d):
    """读取 stage-status.tsv，返回 OrderedDict(stage -> dict)。"""
    rows = OrderedDict()
    txt = read_text(os.path.join(d, "stage-status.tsv"))
    if not txt:
        return rows
    for line in txt.splitlines():
        parts = line.rstrip("\n").split("\t")
        if not parts or parts[0] == "stage" or len(parts) < 2:
            continue
        rows[parts[0]] = {
            "status": parts[1],
            "started_at": parts[2] if len(parts) > 2 else "",
            "finished_at": parts[3] if len(parts) > 3 else "",
        }
    return rows


def read_stages_done(d):
    """返回 (done, partial, missing)。

    done=ok，partial=阶段运行但指标不完整，missing=failed/未开始。
    旧格式没有 partial 概念，第二项返回空列表。
    """
    rows = read_stage_status(d)
    if rows:
        done = [s for s, v in rows.items() if v["status"] == "ok"]
        partial = [s for s, v in rows.items() if v["status"] == "partial"]
        missing = [s for s, v in rows.items() if v["status"] not in ("ok", "partial")]
        requested = read_run_status(d).get("stages_requested", "").split()
        for s in requested:
            if s not in rows and s not in missing:
                missing.append(s)
        return done, partial, missing

    stage_files = dict(LEGACY_STAGE_FILES)
    progress = read_text(os.path.join(d, "progress.log"))
    requested = re.findall(r"阶段开始:\s*(\w+)", progress)
    if not requested:
        requested = [stage for stage, _ in LEGACY_STAGE_FILES]
    failed = set(read_text(os.path.join(d, "failed-stages.txt")).split())
    done, missing = [], []
    for stage in requested:
        fname = stage_files.get(stage)
        if fname and os.path.isfile(os.path.join(d, fname)) and stage not in failed:
            done.append(stage)
        else:
            missing.append(stage)
    return done, [], missing


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def resolve_run_dir(results_dir, name, run_id=None):
    """解析某台主机的运行目录：指定 run_id > LATEST > 旧版扁平目录。"""
    base = os.path.join(results_dir, name)
    if run_id:
        if run_id in (".", "..") or not RUN_ID_RE.match(run_id):
            raise SystemExit(f"[ERR] 非法运行 ID: {run_id!r}")
        return os.path.join(base, run_id)
    latest = read_text(os.path.join(base, "LATEST")).strip()
    if latest:
        candidate = os.path.join(base, latest)
        if os.path.isdir(candidate):
            return candidate
    return base


# ---------------------------------------------------------------------------
# 批次清单与跨 run 聚合
# ---------------------------------------------------------------------------
def read_batch_manifest(path):
    """读取本地批次清单；run_ids 额外解析为列表。"""
    fields = {}
    for line in read_text(path).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            fields[parts[0].strip()] = parts[1].strip()
    fields["run_ids"] = fields.get("run_ids", "").split()
    return fields


def resolve_batch_runs(results_dir, name, batch_id):
    """返回 (manifest, run_ids, manifest_path)；缺失或非法时给出可操作错误。"""
    if not batch_id or not BATCH_ID_RE.match(batch_id):
        raise SystemExit(f"[ERR] 非法批次 ID: {batch_id!r}")
    path = os.path.join(results_dir, name, "batches", f"{batch_id}.tsv")
    if not os.path.isfile(path):
        raise SystemExit(f"[ERR] 本地没有批次清单: {path}（先 collect --batch {batch_id}）")
    manifest = read_batch_manifest(path)
    run_ids = manifest.get("run_ids", [])
    if not run_ids:
        raise SystemExit(f"[ERR] 批次清单缺少 run_ids: {path}")
    if len(set(run_ids)) != len(run_ids):
        raise SystemExit(f"[ERR] 批次清单存在重复 run_id: {path}")
    for run_id in run_ids:
        if not RUN_ID_RE.match(run_id):
            raise SystemExit(f"[ERR] 批次清单含非法运行 ID: {run_id!r}")
    return manifest, run_ids, path


def _nearest_rank(sorted_values, percentile):
    """nearest-rank 分位数：小样本不插值，p95 取实际观测到的上界。"""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def aggregate_metrics(run_metrics):
    """聚合多次 run 的指标分布。

    run_metrics 是每次 run 的原始指标字典列表。只聚合有限数值；缺失值不补 0，
    字符串与控制字段（如 `_pts_*`）不参与。p95 使用 nearest-rank，n 很小
    时会退化为最大值，这是刻意的保守口径。
    """
    values_by_key = {}
    for metrics in run_metrics:
        for key, value in (metrics or {}).items():
            if key.startswith("_") or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            values_by_key.setdefault(key, []).append(number)

    samples = OrderedDict()
    for key, values in values_by_key.items():
        ordered = sorted(values)
        samples[key] = OrderedDict([
            ("n", len(ordered)),
            ("median", statistics.median(ordered)),
            ("p95", _nearest_rank(ordered, 0.95)),
            ("min", ordered[0]),
            ("max", ordered[-1]),
        ])
    return samples


def aggregate_stage_status(run_rows, requested):
    """按 run 聚合阶段状态：全 ok→ok，全 failed→failed，其余→partial。"""
    result = OrderedDict()
    for stage in requested:
        states = []
        for row in run_rows:
            if not row.get("collected"):
                states.append("failed")
            else:
                states.append(row.get("stages", {}).get(stage, "failed"))
        if states and all(state == "ok" for state in states):
            result[stage] = "ok"
        elif states and all(state == "failed" for state in states):
            result[stage] = "failed"
        else:
            result[stage] = "partial"
    return result


def aggregate_batch(results_dir, name, batch_id):
    """读取批次内所有本地 run，返回聚合指标与每个 run 的原始状态。"""
    manifest, run_ids, manifest_path = resolve_batch_runs(results_dir, name, batch_id)
    repeat = safe_int(manifest.get("repeat"), len(run_ids)) or len(run_ids)
    if repeat != len(run_ids):
        raise SystemExit(
            f"[ERR] 批次清单 repeat={repeat} 与 run_ids 数量={len(run_ids)} 不一致: "
            f"{manifest_path}"
        )

    run_rows = []
    run_metrics = []
    present_dirs = []
    requested = manifest.get("stages", "").split()
    started_at = []
    finished_at = []
    steal_samples = 0

    for run_id in run_ids:
        d = os.path.join(results_dir, name, run_id)
        if not os.path.isdir(d):
            run_rows.append(OrderedDict([
                ("run_id", run_id),
                ("collected", False),
                ("status", "missing"),
                ("stages", {}),
                ("started_at", ""),
                ("finished_at", ""),
            ]))
            continue

        present_dirs.append(d)
        metrics, _local, _remote = collect_metrics(d)
        run_metrics.append(metrics)
        run_status = read_run_status(d)
        done, partial, missing = read_stages_done(d)
        stage_map = {stage: "ok" for stage in done}
        stage_map.update({stage: "partial" for stage in partial})
        stage_map.update({stage: "failed" for stage in missing})
        run_requested = run_status.get("stages_requested", "").split()
        for stage in run_requested:
            if stage not in requested:
                requested.append(stage)
        status = run_status.get("status", "") or "unknown"
        run_started = run_status.get("started_at", "")
        run_finished = run_status.get("finished_at", "")
        _tested_at_value = _tested_at(d, run_status)
        if run_started:
            started_at.append(run_started)
        if run_finished:
            finished_at.append(run_finished)
        try:
            steal_samples += int(float(metrics.get("stress_samples", 0)))
        except (TypeError, ValueError):
            pass
        run_rows.append(OrderedDict([
            ("run_id", run_id),
            ("collected", True),
            ("status", status),
            ("stages", stage_map),
            ("started_at", run_started),
            ("finished_at", run_finished),
            ("tested_at", _tested_at_value),
        ]))

    if not requested:
        for row in run_rows:
            for stage in row.get("stages", {}):
                if stage not in requested:
                    requested.append(stage)

    # 每个 run 补齐请求阶段；失败/缺失的 run 明确标 failed，不能静默省略。
    for row in run_rows:
        for stage in requested:
            row["stages"].setdefault(stage, "failed")

    samples = aggregate_metrics(run_metrics)
    median_metrics = {key: detail["median"] for key, detail in samples.items()}
    stages = aggregate_stage_status(run_rows, requested)

    statuses = [row["status"] for row in run_rows]
    if not run_rows or all(status in ("failed", "missing") for status in statuses):
        batch_status = "failed"
    elif all(status == "ok" for status in statuses) and all(
        state == "ok" for state in stages.values()
    ):
        batch_status = "ok"
    else:
        batch_status = "partial"

    first_dir = present_dirs[0] if present_dirs else None
    machine_dir = next(
        (d for d in present_dirs if os.path.isfile(os.path.join(d, "system-info.txt"))),
        first_dir,
    )
    fio_dir = next(
        (d for d in present_dirs if os.path.isfile(os.path.join(d, "fio-4k-randread.json"))),
        first_dir,
    )
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "run_ids": run_ids,
        "repeat": repeat,
        "sample_count": len(present_dirs),
        "ok_run_count": sum(1 for row in run_rows if row["status"] == "ok"),
        "missing_run_ids": [row["run_id"] for row in run_rows if not row["collected"]],
        "run_rows": run_rows,
        "run_metrics": run_metrics,
        "present_dirs": present_dirs,
        "first_dir": first_dir,
        "machine_dir": machine_dir,
        "fio_dir": fio_dir,
        "median_metrics": median_metrics,
        "samples": samples,
        "stages": stages,
        "status": batch_status,
        "started_at": min(started_at) if started_at else "",
        "finished_at": max(finished_at) if finished_at else "",
        "steal_samples": steal_samples,
    }


def read_cpu_model(d):
    txt = read_text(os.path.join(d, "system-info.txt"))
    for line in txt.splitlines():
        if line.strip().startswith("Model name:"):
            return line.split(":", 1)[1].strip()
    for cand in ("yabs-result.json", "yabs_result.json", "result.json"):
        j = load_json(os.path.join(d, cand))
        if j:
            return (j.get("cpu") or {}).get("model", "—")
    return "—"


def _fio_file_info(d):
    j = load_json(os.path.join(d, "fio-4k-randread.json"))
    if not j or not j.get("jobs"):
        return 0, 0
    opts = j["jobs"][0].get("job options") or {}
    size = str(opts.get("size", "")).strip()
    m = re.match(r"^(\d+)\s*([GM])$", size, re.I)
    if not m:
        per_job = 0
    else:
        n, unit = int(m.group(1)), m.group(2).upper()
        per_job = n if unit == "G" else max(1, round(n / 1024))
    jobs = safe_int(opts.get("numjobs"), 1) or 1
    return per_job, per_job * jobs


def _tested_at(d, run_status):
    for key in ("finished_at", "started_at"):
        if run_status.get(key):
            return run_status[key]
    txt = read_text(os.path.join(d, "progress.log"))
    dates = re.findall(r"\[([^\]]+)\]", txt)
    return dates[-1] if dates else ""


def _result_metrics(m):
    """把原始指标映射为站点兼容的 metrics 字段；缺失保持 0。"""
    return {
        "cpu_single_eps": m.get("sysbench_cpu_single_eps", 0),
        "cpu_multi_eps": m.get("sysbench_cpu_multi_eps", 0),
        "sevenzip_compress_mips": m.get("pts_7zip_compress_mips", 0),
        "sevenzip_decompress_mips": m.get("pts_7zip_decompress_mips", 0),
        "kernel_build_sec": m.get("pts_kernel_build_sec", 0),
        "disk_4k_read_iops": m.get("fio_4k_randread_iops", 0),
        "disk_4k_write_iops": m.get("fio_4k_randwrite_iops", 0),
        "disk_4k_read_p99_ms": m.get("fio_4k_randread_p99_ms",
                                      m.get("fio_4k_randread_p99_us", 0) / 1000),
        "disk_4k_read_p999_ms": m.get("fio_4k_randread_p999_ms",
                                       m.get("fio_4k_randread_p999_us", 0) / 1000),
        "disk_4k_write_p99_ms": m.get("fio_4k_randwrite_p99_ms",
                                       m.get("fio_4k_randwrite_p99_us", 0) / 1000),
        "disk_4k_write_p999_ms": m.get("fio_4k_randwrite_p999_ms",
                                        m.get("fio_4k_randwrite_p999_us", 0) / 1000),
        "disk_seq_read_mibs": m.get("fio_seq_read_mib_s", 0),
        "disk_seq_write_mibs": m.get("fio_seq_write_mib_s", 0),
        "mem_bandwidth_mibs": m.get("sysbench_mem_single_mib_s", 0),
        "net_down_mbps": m.get("speedtest_down_mbps", 0),
        "net_up_mbps": m.get("speedtest_up_mbps", 0),
        "net_ping_ms": m.get("speedtest_latency_ms", 0),
        "steal_avg_pct": m.get("stress_avg_steal_pct", 0),
        "redis_rps": m.get("pts_redis_rps", 0),
        "yabs_4k_total_iops": m.get("yabs_4k_total_iops", 0),
    }


def build_result(results_dir, hosts, name, submitter="", run_id=None):
    """组装一台机器的结果对象（与站点 models.go 的 schema 对应）。"""
    h = hosts.get(name)
    if not h:
        raise SystemExit(f"[ERR] hosts.tsv 里没有 '{name}'——先加一行再跑测试")

    d = resolve_run_dir(results_dir, name, run_id)
    if not os.path.isdir(d):
        raise SystemExit(f"[ERR] 运行目录不存在: {d}（先 run + collect，或检查 --run）")
    m, _local, _remote = collect_metrics(d)
    info = read_sysinfo(d)
    run_status = read_run_status(d)
    stages_done, stages_partial, stages_missing = read_stages_done(d)
    per_job_gb, total_gb = _fio_file_info(d)

    vcpu = safe_int(h.get("vcpu"), 0)
    metrics = _result_metrics(m)

    physical_cores = info["physical_cores"] or vcpu
    threads = info["threads"] or vcpu
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "cloud-bench", "version": TOOL_VERSION},
        # 方法论版本当前与工具版本同源；独立字段便于将来只改口径、不改解析器。
        "method_version": TOOL_VERSION,
        "run": {
            "id": run_status.get("run_id", run_id or ""),
            "profile": run_status.get("profile", ""),
            "status": run_status.get("status", ""),
            "started_at": run_status.get("started_at", ""),
            "finished_at": run_status.get("finished_at", ""),
        },
        "machine": {
            "id": name,
            "vendor": h["vendor"],
            "vendor_type": h["vendor_type"],
            "spec": h["spec"],
            "cpu_model": info["cpu_model"],
            "physical_cores": physical_cores,
            "physical_cores_source": "lscpu" if info["physical_cores"] else "vcpu_fallback",
            "threads": threads,
            "avx512": info["avx512"],
            "ram_gb": safe_int(h["ram_gb"], 0),
            "bandwidth": h["bandwidth"],
            "price_cny": safe_int(h["price_cny"], 0),
            "price_note": h["price_note"],
            "region": h["region"],
            "role": h["role"],
            "kernel": info["kernel"],
            "virtualization": info["virtualization"],
            "governor": info["governor"],
            "scheduler": info["scheduler"],
            "loadavg": info["loadavg"],
            "container_count": info["container_count"],
            "tested_at": _tested_at(d, run_status),
            "submitter": submitter,
            "fio_file_size_gb": per_job_gb,
            "fio_total_size_gb": total_gb,
        },
        "metrics": metrics,
        # mpstat 有效样本数：0% steal 只有在 samples > 0 时才是「实测结论」。
        "steal_samples": m.get("stress_samples", 0),
        "stages_done": stages_done,
        "stages_partial": stages_partial,
        "stages_missing": stages_missing,
        # 服务端会按实际指标重算该字段；这里填的是本地视角
        "metrics_missing": [k for k in ZERO_MEANS_MISSING if not metrics.get(k)],
    }

def build_batch_result(results_dir, hosts, name, batch_id, submitter=""):
    """组装多次采样的聚合结果；metrics 取中位数，samples 保留分布。"""
    h = hosts.get(name)
    if not h:
        raise SystemExit(f"[ERR] hosts.tsv 里没有 '{name}'——先加一行再跑测试")

    agg = aggregate_batch(results_dir, name, batch_id)
    first_dir = agg["first_dir"]
    if not first_dir:
        raise SystemExit(
            f"[ERR] 批次 {batch_id} 没有已回收的 run 目录（先 collect --batch {batch_id}）"
        )

    info = read_sysinfo(agg["machine_dir"])
    per_job_gb, total_gb = _fio_file_info(agg["fio_dir"])
    metrics = _result_metrics(agg["median_metrics"])
    vcpu = safe_int(h.get("vcpu"), 0)
    physical_cores = info["physical_cores"] or vcpu
    threads = info["threads"] or vcpu

    stages = agg["stages"]
    stages_done = [stage for stage, state in stages.items() if state == "ok"]
    stages_partial = [stage for stage, state in stages.items() if state == "partial"]
    stages_missing = [stage for stage, state in stages.items() if state == "failed"]
    tested_at_values = [row.get("tested_at", "") for row in agg["run_rows"]]
    tested_at = max((value for value in tested_at_values if value), default="")
    profile = agg["manifest"].get("profile", "")
    present_count = len(agg["present_dirs"])
    ok_count = sum(1 for row in agg["run_rows"] if row["status"] == "ok")
    missing_run_ids = [row["run_id"] for row in agg["run_rows"] if not row["collected"]]

    batch = OrderedDict([
        ("id", batch_id),
        ("repeat", agg["repeat"]),
        ("status", agg["status"]),
        ("run_ids", agg["run_ids"]),
        ("sample_count", present_count),
        ("sample_expected", agg["repeat"]),
        ("ok_run_count", ok_count),
        ("missing_run_ids", missing_run_ids),
        ("created_at", agg["manifest"].get("created_at", "")),
        ("profile", profile),
        ("stages", stages),
        ("runs", agg["run_rows"]),
    ])

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "cloud-bench", "version": TOOL_VERSION},
        "method_version": TOOL_VERSION,
        "run": {
            "id": batch_id,
            "profile": profile,
            "status": agg["status"],
            "started_at": agg["started_at"],
            "finished_at": agg["finished_at"],
        },
        "batch": batch,
        "machine": {
            "id": name,
            "vendor": h["vendor"],
            "vendor_type": h["vendor_type"],
            "spec": h["spec"],
            "cpu_model": info["cpu_model"],
            "physical_cores": physical_cores,
            "physical_cores_source": "lscpu" if info["physical_cores"] else "vcpu_fallback",
            "threads": threads,
            "avx512": info["avx512"],
            "ram_gb": safe_int(h["ram_gb"], 0),
            "bandwidth": h["bandwidth"],
            "price_cny": safe_int(h["price_cny"], 0),
            "price_note": h["price_note"],
            "region": h["region"],
            "role": h["role"],
            "kernel": info["kernel"],
            "virtualization": info["virtualization"],
            "governor": info["governor"],
            "scheduler": info["scheduler"],
            "loadavg": info["loadavg"],
            "container_count": info["container_count"],
            "tested_at": tested_at,
            "submitter": submitter,
            "fio_file_size_gb": per_job_gb,
            "fio_total_size_gb": total_gb,
        },
        "metrics": metrics,
        "samples": agg["samples"],
        "steal_samples": agg["steal_samples"],
        "stages_done": stages_done,
        "stages_partial": stages_partial,
        "stages_missing": stages_missing,
        "metrics_missing": [k for k in ZERO_MEANS_MISSING if not metrics.get(k)],
    }
