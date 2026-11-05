#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多云轻量主机性能横评 —— 汇总脚本

读取 results/hosts.tsv 主机清单 + 各主机目录下的**原始产物**，通过
`bench_metrics.py` 统一解析后生成：
    results/summary/compare.csv   机器可读
    results/summary/compare.md    Markdown 表格
    results/summary/compare.json  结构化数据

以及单机「人话报告」：
    ./scripts/cloud-benchmark.sh report --name X

运行目录支持两种布局：
    results/<name>/<run_id>/   （新版，推荐；LATEST 文件指向最近一次）
    results/<name>/            （旧版扁平目录，向后兼容）

多次采样时使用 `--batch <id>`：汇总表/报告读取
`results/<name>/batches/<id>.tsv`，指标取各 run 的中位数，并在
`compare.json` / 报告里保留 p95、范围与样本数 n。
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_metrics as bm  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.environ.get("CB_RESULTS_DIR") or os.path.join(REPO_ROOT, "results")
HOSTS = os.path.join(RESULTS, "hosts.tsv")
OUT_DIR = os.path.join(RESULTS, "summary")

METRIC_COLS = bm.METRIC_COLS

# 阈值取自 2026-09 六台轻量云实测（腾讯云×4 / 火山云 / 天翼云）：
#   4K 读 IOPS   <5,000 偏弱 | 5,000~15,000 一般 | >15,000 良好
#   p99 延迟     >100ms 偏弱 | 20~100ms 一般    | <20ms 良好
#   满载 %steal  >5% 偏弱   | 1~5% 注意         | <1% 良好
#   单核         <400 偏弱  | 400~600 中等      | >600 良好
P99_GOOD_MS, P99_OK_MS = 20, 100
IOPS_GOOD, IOPS_OK = 15000, 5000
SINGLE_GOOD, SINGLE_OK = 600, 400
STEAL_OK, STEAL_WARN = 1, 5


def _g_bigger(v, ok, good):
    """越大越好的指标 → (等级, 标记)。"""
    if v is None:
        return "未测", ""
    if v >= good:
        return "良好", "✅"
    return ("一般", "") if v >= ok else ("偏弱", "⚠️")


def _g_smaller(v, ok, good):
    """越小越好的指标（延迟类）→ (等级, 标记)。"""
    if v is None:
        return "未测", ""
    if v <= good:
        return "良好", "✅"
    return ("一般", "") if v <= ok else ("偏弱", "⚠️")


def _dispw(s):
    """字符串的终端显示宽度：CJK 与 emoji 占 2 列，其余占 1 列。"""
    w = 0
    for c in str(s):
        o = ord(c)
        if o in (0xFE0F, 0xFE0E, 0x200D):
            continue
        w += 2 if (o > 0x2E80 or 0x2190 <= o <= 0x2BFF or 0x1F300 <= o <= 0x1FAFF) else 1
    return w


def _pad(s, width, right=False):
    """按显示宽度补齐到 width 列。"""
    s = str(s)
    gap = " " * max(0, width - _dispw(s))
    return gap + s if right else s + gap


def _mem_actual(d):
    """从 free -h 的输出读实测内存（GB）。"""
    txt = bm.read_text(os.path.join(d, "system-info.txt"))
    m = re.search(r"^Mem:\s+([\d.]+)\s*([GM])i?", txt, re.M)
    if not m:
        return None
    v = float(m.group(1))
    return v if m.group(2) == "G" else v / 1024.0


def _what_can_it_run(ram, g_iops, g_p99, g_cpu, g_steal):
    """把指标等级翻译成「能跑什么」——未测项不参与推荐。"""
    can, warn, cannot = [], [], []

    if ram >= 2:
        can.append("个人博客 / 静态站 / 图床")
    elif ram > 0:
        warn.append("小内存（<2G）：只够纯静态页，别装面板")
    else:
        warn.append("内存容量未识别，不能判断能跑多少服务")

    if g_cpu[0] == "未测":
        warn.append("CPU 未测，不能判断容器数量")
    elif ram >= 2 and g_cpu[0] != "偏弱":
        can.append("5~10 个轻量容器（面板 / Nginx / 小服务）")
    elif ram >= 2:
        warn.append("小容器 3~5 个（CPU 偏弱，多开会卡）")

    if g_p99[0] == "未测" or g_iops[0] == "未测":
        warn.append("磁盘未测，不能判断数据库是否适合")
    elif g_p99[0] == "良好" and g_iops[0] != "偏弱":
        can.append("轻量数据库（MySQL / PostgreSQL / Redis）")
    elif g_p99[0] == "偏弱":
        warn.append("数据库：能跑，但磁盘长尾会带来偶发卡顿（建站时 MySQL 最明显）")
    else:
        warn.append("数据库：磁盘一般，轻量使用尚可")

    if g_cpu[0] == "良好" and ram >= 4:
        can.append("中小项目编译")
    else:
        cannot.append("编译构建（Go / Rust / 大型 npm 项目）")
    cannot.append("视频转码 / AI 推理 / 高并发站点")

    if g_steal[0] == "偏弱":
        warn.append("⚠️ 检测到明显超售——性能可能随邻居负载波动")
    elif g_steal[0] == "未测":
        warn.append("超售未测，不能判断邻居是否抢资源")
    return can, warn, cannot


def _fmt(val, ndigits):
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):,.{ndigits}f}"
    except (TypeError, ValueError):
        return str(val)


def main(run_id="", batch_id=""):
    host_list = bm.read_hosts(HOSTS)
    if not host_list:
        print("[ERR] 没有可汇总的主机，检查 results/hosts.tsv", file=sys.stderr)
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    rows, raw = [], []
    notes = []
    found_batch = 0

    for h in host_list:
        batch_agg = None
        samples = {}
        local, remote = {}, {}
        if batch_id:
            manifest_path = os.path.join(RESULTS, h["name"], "batches", f"{batch_id}.tsv")
            if not os.path.isfile(manifest_path):
                continue
            batch_agg = bm.aggregate_batch(RESULTS, h["name"], batch_id)
            if not batch_agg["present_dirs"]:
                notes.append(f"{h['name']}：批次 {batch_id} 尚未回收任何 run")
                continue
            found_batch += 1
            d = batch_agg["machine_dir"]
            m = batch_agg["median_metrics"]
            samples = batch_agg["samples"]
            done = [s for s, v in batch_agg["stages"].items() if v == "ok"]
            partial = [s for s, v in batch_agg["stages"].items() if v == "partial"]
            missing = [s for s, v in batch_agg["stages"].items() if v == "failed"]
            run_status = {
                "run_id": batch_id,
                "status": batch_agg["status"],
                "profile": batch_agg["manifest"].get("profile", ""),
            }
            if batch_agg["status"] != "ok":
                notes.append(f"{h['name']}：批次 {batch_id} 状态 {batch_agg['status']}，"
                             f"样本 {batch_agg['sample_count']}/{batch_agg['repeat']}")
            if batch_agg["sample_count"] < batch_agg["repeat"]:
                missing_ids = [r["run_id"] for r in batch_agg["run_rows"] if not r["collected"]]
                notes.append(f"{h['name']}：批次样本不足 "
                             f"{batch_agg['sample_count']}/{batch_agg['repeat']}"
                             + (f"，缺失 {', '.join(missing_ids)}" if missing_ids else ""))
            short_metrics = sorted(
                key for key, detail in samples.items()
                if detail.get("n", 0) < batch_agg["repeat"]
            )
            if short_metrics:
                shown = ", ".join(short_metrics[:6]) + (" ..." if len(short_metrics) > 6 else "")
                notes.append(f"{h['name']}：{len(short_metrics)} 项指标样本数少于 "
                             f"{batch_agg['repeat']}（{shown}）")
        else:
            d = bm.resolve_run_dir(RESULTS, h["name"], run_id or None)
            m, local, remote = bm.collect_metrics(d)
            done, partial, missing = bm.read_stages_done(d)
            run_status = bm.read_run_status(d)

        cpu_model = bm.read_cpu_model(d)
        try:
            price_month = float(h["price_cny"]) / 12.0
        except ValueError:
            price_month = None

        if m.get("_pts_batch_unconfigured") or m.get("_pts_only_downloaded"):
            notes.append(f"{h['name']}：PTS 未产出成绩（batch 模式未配置或仅完成下载）")
        if run_status.get("status") in ("failed", "partial"):
            detail = []
            if partial:
                detail.append(f"部分完成：{', '.join(partial)}")
            if missing:
                detail.append(f"缺失阶段：{', '.join(missing)}")
            notes.append(f"{h['name']}：运行状态 {run_status['status']}，"
                         + ("；".join(detail) if detail else "无阶段明细"))

        row = OrderedDict()
        if batch_id:
            row["批次"] = batch_id
        row["云商"] = h["vendor"]
        row["规格"] = h["spec"]
        row["CPU型号"] = cpu_model
        row["vCPU"] = h["vcpu"]
        row["带宽"] = h["bandwidth"]
        row["首年价(元)"] = h["price_cny"]
        row["折合月价(元)"] = _fmt(price_month, 2) if price_month else "—"

        for label, key, nd in METRIC_COLS:
            row[label] = _fmt(m.get(key), nd)

        if price_month:
            row["CPU性价比(多核/月价)"] = _fmt(float(m["sysbench_cpu_multi_eps"]) / price_month, 1) \
                if m.get("sysbench_cpu_multi_eps") else "—"
            row["IOPS性价比(读/月价)"] = _fmt(float(m["fio_4k_randread_iops"]) / price_month, 0) \
                if m.get("fio_4k_randread_iops") else "—"
        else:
            row["CPU性价比(多核/月价)"] = "—"
            row["IOPS性价比(读/月价)"] = "—"

        total_stages = len(done) + len(partial) + len(missing)
        completeness = f"{len(done)}/{total_stages}"
        if partial:
            completeness += f"（部分 {','.join(partial)}）"
        if missing:
            completeness += f"（缺 {','.join(missing)}）"
        if batch_id:
            row["采样数"] = f"{batch_agg['sample_count']}/{batch_agg['repeat']}"
            row["批状态"] = batch_agg["status"]
        row["数据完整度"] = completeness
        row["运行状态"] = run_status.get("status", "旧版目录")
        row["备注"] = h["role"]
        rows.append(row)
        raw.append({
            "host": h,
            "run_dir": d,
            "run": run_status,
            "metrics": m,
            "local_parsed": {} if batch_id else local,
            "remote_metrics": {} if batch_id else remote,
            "samples": samples,
            "batch": batch_agg,
            "stages_done": done,
            "stages_partial": partial,
            "stages_missing": missing,
        })

    if batch_id and found_batch == 0:
        print(f"[ERR] 没有任何主机的本地批次清单: {batch_id}（先 collect --batch）",
              file=sys.stderr)
        return 1

    columns = list(rows[0].keys())

    csv_path = os.path.join(OUT_DIR, "compare.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

    md_path = os.path.join(OUT_DIR, "compare.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("# 多云轻量主机性能横评 —— 对比表\n\n")
        fh.write("> 价格口径：**首年特惠价**，性价比按 首年价÷12 折算月价计算，非续费价。\n")
        fh.write("> 数据由 `scripts/bench_metrics.py` 从各主机原始产物统一解析生成。\n")
        if batch_id:
            fh.write(f"> 本表为批次 `{batch_id}` 的多次采样中位数；分布见 compare.json 的 `samples`。\n")
        fh.write("\n")
        fh.write("| " + " | ".join(columns) + " |\n")
        fh.write("|" + "|".join(["---"] * len(columns)) + "|\n")
        for r in rows:
            fh.write("| " + " | ".join(str(r[c]) for c in columns) + " |\n")
        fh.write("\n## 关键结论\n\n")
        for metric in ("单核(events/s)", "多核(events/s)", "4K读IOPS", "满载%steal(均)", "上行(Mbps)"):
            valid = [(r["云商"], r[metric]) for r in rows if r[metric] != "—"]
            if len(valid) >= 2:
                def num(x):
                    return float(str(x[1]).replace(",", ""))
                try:
                    hi = max(valid, key=num)
                    lo = min(valid, key=num)
                    fh.write(f"- **{metric}**：最高 {hi[0]}（{hi[1]}），最低 {lo[0]}（{lo[1]}）\n")
                except ValueError:
                    pass
        p99 = [(r["云商"], r["4K读p99(ms)"]) for r in rows if r["4K读p99(ms)"] != "—"]
        if len(p99) >= 2:
            try:
                worst = max(p99, key=lambda x: float(str(x[1]).replace(",", "")))
                best = min(p99, key=lambda x: float(str(x[1]).replace(",", "")))
                fh.write(f"- **4K读p99(ms)**：最低 {best[0]}（{best[1]}），最高 {worst[0]}（{worst[1]}）\n")
            except ValueError:
                pass
        if notes:
            fh.write("\n## 数据说明\n\n")
            for n in notes:
                fh.write(f"- {n}\n")
        fh.write("\n")
        gb = [(r["云商"], raw[i]["metrics"].get("yabs_geekbench_url"))
              for i, r in enumerate(rows) if raw[i]["metrics"].get("yabs_geekbench_url")]
        if gb:
            fh.write("### Geekbench 6 结果链接\n\n")
            for vendor, url in gb:
                fh.write(f"- {vendor}: {url}\n")

    json_path = os.path.join(OUT_DIR, "compare.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"columns": columns, "rows": rows, "raw": raw},
                  fh, ensure_ascii=False, indent=2, default=str)

    print(f"[ OK ] 已生成对比表（{len(rows)} 台主机）")
    for p in (csv_path, md_path, json_path):
        print(f"       {p}")
    for r in rows:
        print(f"  - {r['云商']:<6} {r['规格']:<6} 数据完整度={r['数据完整度']}")
    for n in notes:
        print(f"  [!] {n}")
    return 0

def render_report(name, run_id=None, batch_id=None):
    """单机人话报告：./scripts/cloud-benchmark.sh report --name X [--run ID | --batch ID]"""
    if batch_id:
        return render_batch_report(name, batch_id)
    d = bm.resolve_run_dir(RESULTS, name, run_id)
    m, _local, _remote = bm.collect_metrics(d)
    hosts = bm.hosts_by_name(HOSTS)
    h = hosts.get(name, {})
    if not m and not os.path.isdir(d):
        print(f"[ERR] 没有 {name} 的产物，先跑 run + collect", file=sys.stderr)
        return 1

    p99 = m.get("fio_4k_randread_p99_ms")
    if p99 is None and m.get("fio_4k_randread_p99_us") is not None:
        p99 = m["fio_4k_randread_p99_us"] / 1000.0
    # 这些指标 0 只可能是未跑/解析失败；%steal 的 0 是有效结论，单独处理。
    iops = m.get("fio_4k_randread_iops") or None
    single = m.get("sysbench_cpu_single_eps") or None
    multi = m.get("sysbench_cpu_multi_eps") or None
    steal = m.get("stress_avg_steal_pct")
    if p99 == 0:
        p99 = None
    ram = bm.safe_int(h.get("ram_gb"), 0)
    info = bm.read_sysinfo(d)
    cores, threads = info["physical_cores"], info["threads"]
    run_status = bm.read_run_status(d)
    done, partial, missing = bm.read_stages_done(d)

    g_iops = _g_bigger(iops, IOPS_OK, IOPS_GOOD)
    g_p99 = _g_smaller(p99, P99_OK_MS, P99_GOOD_MS)
    g_cpu = _g_bigger(single, SINGLE_OK, SINGLE_GOOD)
    g_steal = _g_smaller(steal, STEAL_WARN, STEAL_OK)
    can, warn, cannot = _what_can_it_run(ram, g_iops, g_p99, g_cpu, g_steal)

    W = 62
    out = []
    out.append("═" * W)
    head = f"  {h.get('vendor','?')} {h.get('spec','?')}"
    price, note = str(h.get("price_cny") or ""), h.get("price_note") or ""
    if price and price != "0" and not note.startswith("待补"):
        head += f" · ¥{price}" + (f"/{note}" if note else "")
    if h.get("region"):
        head += f" · {h['region']}"
    out.append(head)
    if h.get("role"):
        out.append(f"  用途：{h['role']}")
    if run_status.get("run_id") or run_status.get("status"):
        out.append(f"  运行：{run_status.get('run_id', '?')}  "
                   f"状态={run_status.get('status', '?')}")
    out.append("═" * W)

    if partial or missing:
        detail = []
        if partial:
            detail.append(f"部分完成：{', '.join(partial)}")
        if missing:
            detail.append(f"缺失：{', '.join(missing)}")
        out.append(f"  ⚠️ 本次数据不完整（{'；'.join(detail)}）——以下结论仅作初步参考")

    def line(label, value, unit, g):
        mark = g[1] or "  "
        out.append(f"  {_pad(label,16)}{_pad(value,13,right=True)} {_pad(unit,10)}{mark} {g[0]}")

    out.append("")
    out.append("【会不会卡？】← 轻量云最该看的一项")
    line("磁盘 4K 随机读", _fmt(iops, 0), "IOPS", g_iops)
    line("磁盘长尾 p99", _fmt(p99, 2), "ms", g_p99)
    if g_p99[0] == "偏弱":
        out.append("  → ⚠️ 长尾严重：约 1% 的请求要等几百毫秒，建站会偶发卡顿")
    elif g_p99[0] == "良好":
        out.append("  → 日常使用流畅，不太可能感到卡顿")
    elif g_p99[0] == "未测":
        out.append("  → 本次没有 p99 数据，不能判断卡顿；先看 fio 阶段是否失败")
    else:
        out.append("  → 基本流畅，重负载下偶有延迟")

    out.append("")
    out.append("【有没有被超售？】")
    line("满载 %steal", _fmt(steal, 2), "%", g_steal)
    if g_steal[0] == "良好":
        out.append("  → 未检测到明显抢占（部分虚拟化平台可能不暴露真实 %steal）")
    elif g_steal[0] == "未测":
        out.append("  → 本次没有 %steal 数据，不能判断是否超售")
    else:
        out.append("  → 存在资源争抢")

    out.append("")
    out.append("【CPU 够用吗？】")
    line("单核性能", _fmt(single, 0), "events/s", g_cpu)
    if cores and threads:
        virt = "✅ 未虚标" if cores == threads else "⚠️ 超线程"
        out.append(f"  {_pad('物理核',16)}{_pad(cores,13,right=True)} 核（{threads} 线程）  {virt}")
        if cores != threads:
            out.append(f"  → 标称 {threads} 核，实际只有 {cores} 个物理核")
    else:
        out.append("  → 物理核数未识别（lscpu 信息缺失）")
    if multi:
        out.append(f"  {_pad('多核性能',16)}{_pad(_fmt(multi,0),13,right=True)} events/s")

    out.append("")
    out.append("【内存与网络】")
    mem_act = _mem_actual(d)
    out.append(f"  {_pad('内存',16)}{_pad(ram,13,right=True)} GB（标称）")
    if mem_act and ram and mem_act < ram * 0.92:
        out.append(f"  → ⚠️ 实测可用仅 {mem_act:.1f}G，比标称少 {round((1 - mem_act / ram) * 100)}%"
                   "（内核/虚拟化占用）——按实测值规划能跑多少服务")
    elif mem_act:
        out.append(f"  → ✅ 实测可用 {mem_act:.1f}G，与标称相符")
    line("内存带宽", _fmt(m.get("sysbench_mem_single_mib_s"), 0), "MiB/s", ("", ""))
    up, down = m.get("speedtest_up_mbps"), m.get("speedtest_down_mbps")
    if up or down:
        bw = h.get("bandwidth", "")
        out.append(f"  {_pad('上行 / 下行',16)}{_pad(_fmt(up,2) + ' / ' + _fmt(down,2),13,right=True)} Mbps")
        if bw:
            try:
                if down and float(down) >= float(bw.rstrip("M")) * 0.8:
                    out.append(f"  → ✅ 达标（套餐 {bw}）")
                elif down:
                    out.append(f"  → ⚠️ 未跑满套餐标称的 {bw}")
            except ValueError:
                pass

    out.append("")
    out.append("【这台能跑什么？】")
    for x in can:
        out.append(f"  ✅ {x}")
    for x in warn:
        out.append(f"  ⚠️ {x}")
    for x in cannot:
        out.append(f"  ❌ {x}")

    total = len(done) + len(partial) + len(missing)
    out.append("")
    out.append(f"【数据完整度】{len(done)}/{total} 阶段完全完成"
               + (f"；部分：{', '.join(partial)}" if partial else "")
               + (f"；缺：{', '.join(missing)}" if missing else ""))
    if run_status.get("status"):
        out.append(f"【运行状态】{run_status['status']}")
    out.append("")

    print("\n".join(out))
    return 0


def _sample_for(samples, keys, scale=1.0):
    """从聚合 samples 中取第一个可用指标，并可按单位换算。"""
    for key in keys:
        detail = samples.get(key)
        if not detail:
            continue
        effective_scale = scale if key.endswith("_us") else 1.0
        return {k: (v * effective_scale if k in ("median", "p95", "min", "max") else v)
                for k, v in detail.items()}
    return None


def _sample_text(detail, ndigits):
    if not detail:
        return "—"
    return (f"{_fmt(detail['median'], ndigits)}（p95 {_fmt(detail['p95'], ndigits)}，"
            f"范围 {_fmt(detail['min'], ndigits)}~{_fmt(detail['max'], ndigits)}，"
            f"n={detail['n']}）")


def render_batch_report(name, batch_id):
    """多次采样的「人话报告」：中位数用于结论，p95/范围/n 展示波动。"""
    hosts = bm.hosts_by_name(HOSTS)
    h = hosts.get(name, {})
    result = bm.build_batch_result(RESULTS, hosts, name, batch_id)
    m = result["metrics"]
    samples = result["samples"]
    batch = result["batch"]
    machine = result["machine"]

    iops = m.get("disk_4k_read_iops") or None
    p99 = m.get("disk_4k_read_p99_ms") or None
    single = m.get("cpu_single_eps") or None
    multi = m.get("cpu_multi_eps") or None
    steal = m.get("steal_avg_pct") if "stress_avg_steal_pct" in samples else None
    ram = bm.safe_int(h.get("ram_gb"), 0)
    cores, threads = machine.get("physical_cores", 0), machine.get("threads", 0)

    g_iops = _g_bigger(iops, IOPS_OK, IOPS_GOOD)
    g_p99 = _g_smaller(p99, P99_OK_MS, P99_GOOD_MS)
    g_cpu = _g_bigger(single, SINGLE_OK, SINGLE_GOOD)
    g_steal = _g_smaller(steal, STEAL_WARN, STEAL_OK)
    can, warn, cannot = _what_can_it_run(ram, g_iops, g_p99, g_cpu, g_steal)

    W = 62
    out = []
    out.append("═" * W)
    head = f"  {h.get('vendor','?')} {h.get('spec','?')}"
    price, note = str(h.get("price_cny") or ""), h.get("price_note") or ""
    if price and price != "0" and not note.startswith("待补"):
        head += f" · ¥{price}" + (f"/{note}" if note else "")
    if h.get("region"):
        head += f" · {h['region']}"
    out.append(head)
    out.append(f"  批次：{batch_id}  {batch['sample_count']}/{batch['repeat']} 个样本  "
               f"状态={batch['status']}")
    out.append("  口径：中位数用于结论；括号内是 p95 / 范围 / n")
    if batch["sample_count"] < batch["repeat"]:
        out.append(f"  ⚠️ 样本数不足 {batch['repeat']}，聚合只使用已回收样本")
        if batch.get("missing_run_ids"):
            out.append(f"     缺失 run：{', '.join(batch['missing_run_ids'])}")
    short_metrics = sorted(
        key for key, detail in samples.items() if detail.get("n", 0) < batch["repeat"]
    )
    if short_metrics:
        shown = ", ".join(short_metrics[:6]) + (" ..." if len(short_metrics) > 6 else "")
        out.append(f"  ⚠️ {len(short_metrics)} 项指标样本数少于 {batch['repeat']}（{shown}）")
    if batch["status"] != "ok":
        out.append(f"  ⚠️ 批次状态为 {batch['status']}，结论需结合每个 run 的状态")
    out.append("═" * W)

    out.append("")
    out.append("【会不会卡？】← 轻量云最该看的一项")
    out.append(f"  {_pad('磁盘 4K 随机读',16)}{_sample_text(_sample_for(samples, ['fio_4k_randread_iops']), 0)} IOPS  {g_iops[1]} {g_iops[0]}")
    p99_sample = _sample_for(samples, ["fio_4k_randread_p99_ms", "fio_4k_randread_p99_us"], 0.001)
    out.append(f"  {_pad('磁盘长尾 p99',16)}{_sample_text(p99_sample, 2)} ms  {g_p99[1]} {g_p99[0]}")
    if g_p99[0] == "偏弱":
        out.append("  → ⚠️ 长尾严重：约 1% 的请求要等几百毫秒，建站会偶发卡顿")
    elif g_p99[0] == "良好":
        out.append("  → 日常使用流畅，不太可能感到卡顿")
    elif g_p99[0] == "未测":
        out.append("  → 本次没有 p99 数据，不能判断卡顿；先看 fio 阶段是否失败")
    else:
        out.append("  → 基本流畅，重负载下偶有延迟")

    out.append("")
    out.append("【有没有被超售？】")
    out.append(f"  {_pad('满载 %steal',16)}{_sample_text(_sample_for(samples, ['stress_avg_steal_pct']), 2)} %  {g_steal[1]} {g_steal[0]}")
    if g_steal[0] == "良好":
        out.append("  → 未检测到明显抢占（部分虚拟化平台可能不暴露真实 %steal）")
    elif g_steal[0] == "未测":
        out.append("  → 本次没有 %steal 数据，不能判断是否超售")
    else:
        out.append("  → 存在资源争抢")

    out.append("")
    out.append("【CPU 够用吗？】")
    out.append(f"  {_pad('单核性能',16)}{_sample_text(_sample_for(samples, ['sysbench_cpu_single_eps']), 0)} events/s  {g_cpu[1]} {g_cpu[0]}")
    if cores and threads:
        virt = "✅ 未虚标" if cores == threads else "⚠️ 超线程"
        out.append(f"  {_pad('物理核',16)}{_pad(cores,13,right=True)} 核（{threads} 线程）  {virt}")
        if cores != threads:
            out.append(f"  → 标称 {threads} 核，实际只有 {cores} 个物理核")
    else:
        out.append("  → 物理核数未识别（lscpu 信息缺失）")
    if multi:
        out.append(f"  {_pad('多核性能',16)}{_sample_text(_sample_for(samples, ['sysbench_cpu_multi_eps']), 0)} events/s")

    out.append("")
    out.append("【内存与网络】")
    out.append(f"  {_pad('内存',16)}{_pad(ram,13,right=True)} GB（标称）")
    mem_detail = _sample_for(samples, ["sysbench_mem_single_mib_s"])
    if mem_detail:
        out.append(f"  {_pad('内存带宽',16)}{_sample_text(mem_detail, 0)} MiB/s")
    up = m.get("net_up_mbps") or None
    down = m.get("net_down_mbps") or None
    if up or down:
        out.append(f"  {_pad('上行 / 下行',16)}{_pad(_fmt(up,2) + ' / ' + _fmt(down,2),13,right=True)} Mbps（中位数）")
    net_detail = _sample_for(samples, ["speedtest_down_mbps"])
    if net_detail:
        out.append(f"  {_pad('下行分布',16)}{_sample_text(net_detail, 2)} Mbps")

    out.append("")
    out.append("【这台能跑什么？】")
    for x in can:
        out.append(f"  ✅ {x}")
    for x in warn:
        out.append(f"  ⚠️ {x}")
    for x in cannot:
        out.append(f"  ❌ {x}")

    stage_total = len(result["stages_done"]) + len(result["stages_partial"]) + len(result["stages_missing"])
    out.append("")
    out.append(f"【阶段聚合】{len(result['stages_done'])}/{stage_total} 个阶段在全部样本中成功"
               + (f"；部分：{', '.join(result['stages_partial'])}" if result["stages_partial"] else "")
               + (f"；失败：{', '.join(result['stages_missing'])}" if result["stages_missing"] else ""))
    bad_runs = [r for r in batch["runs"] if r["status"] != "ok"]
    if bad_runs:
        out.append("【样本状态】" + "，".join(f"{r['run_id']}={r['status']}" for r in bad_runs))
    out.append("")

    print("\n".join(out))
    return 0


def _parse_run_arg(args):
    run_id = ""
    if "--run" in args:
        i = args.index("--run")
        if i + 1 >= len(args):
            raise SystemExit("--run 需要一个运行 ID")
        run_id = args[i + 1]
        args = args[:i] + args[i + 2:]
    return args, run_id


def _parse_batch_arg(args):
    batch_id = ""
    if "--batch" in args:
        i = args.index("--batch")
        if i + 1 >= len(args):
            raise SystemExit("--batch 需要一个批次 ID")
        batch_id = args[i + 1]
        args = args[:i] + args[i + 2:]
    return args, batch_id


if __name__ == "__main__":
    argv, run_id = _parse_run_arg(sys.argv[1:])
    argv, batch_id = _parse_batch_arg(argv)
    if run_id and batch_id:
        raise SystemExit("--run 与 --batch 不能同时使用")
    if len(argv) >= 2 and argv[0] == "--report":
        sys.exit(render_report(argv[1], run_id or None, batch_id or None))
    sys.exit(main(run_id, batch_id))
