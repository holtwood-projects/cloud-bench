#!/usr/bin/env python3
"""把评测产物转换成结果 JSON —— 可直接提交到 Cloud Bench 站点。

用法::

    # 只生成 JSON（默认输出到 stdout）
    ./scripts/to-result.py --name mycloud-2c2g-01 -o result.json

    # 指定某次运行
    ./scripts/to-result.py --name mycloud-2c2g-01 --run 20261105T120000Z-abc123

    # 多次采样的批次（metrics=中位数，samples=分布）
    ./scripts/to-result.py --name mycloud-2c2g-01 --batch 20261105T120000Z-abc123

    # 生成并提交到站点
    ./scripts/to-result.py --name mycloud-2c2g-01 --submit https://bench.example.com

环境变量::

    BENCH_API_KEY   提交密钥。**推荐用环境变量**——写成 --key 参数会留在 shell 历史。

机器元信息（云商 / 规格 / 价格 / 地区）从 ``results/hosts.tsv`` 读，
即你已经为跑测试填好的那份清单，不需要再填一遍。

解析统一走 ``bench_metrics.py``：原始产物优先，``metrics.tsv`` 只作旧版兜底。
这样 report / submit 对同一份运行使用同一组数字。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_metrics as bm  # noqa: E402


def post_result(url, key, payload):
    """提交到站点。失败时给出可操作的提示，而不是只抛一个 HTTP 码。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/submit", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return -1, f"网络错误: {e.reason}"


def main():
    ap = argparse.ArgumentParser(description="把评测产物转成结果 JSON（可提交到站点）")
    ap.add_argument("--name", required=True, help="主机名（对应 hosts.tsv 与 results/<name>/）")
    run_group = ap.add_mutually_exclusive_group()
    run_group.add_argument("--run", default="", help="运行 ID（默认取 results/<name>/LATEST）")
    run_group.add_argument("--batch", default="", help="批次 ID（使用多次采样聚合值）")
    ap.add_argument("--results", default="results", help="产物根目录（默认 ./results）")
    ap.add_argument("--hosts", default="", help="主机清单路径（默认 <results>/hosts.tsv）")
    ap.add_argument("-o", "--output", default="", help="输出文件（默认打印到 stdout）")
    ap.add_argument("--submit", default="", metavar="URL", help="生成后直接提交到该站点")
    ap.add_argument("--key", default="", help="提交密钥（建议改用 BENCH_API_KEY 环境变量）")
    ap.add_argument("--submitter", default="", help="提交者标识（默认取 git user.name）")
    args = ap.parse_args()

    results_dir = args.results
    hosts_file = args.hosts or os.path.join(results_dir, "hosts.tsv")
    submitter = args.submitter
    if not submitter:
        try:
            submitter = subprocess.run(["git", "config", "user.name"], capture_output=True,
                                       text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            submitter = ""

    hosts = bm.hosts_by_name(hosts_file)
    if args.batch:
        result = bm.build_batch_result(
            results_dir, hosts, args.name, args.batch, submitter=submitter,
        )
    else:
        result = bm.build_result(
            results_dir, hosts, args.name,
            submitter=submitter, run_id=args.run or None,
        )

    missing = result["metrics_missing"]
    if missing:
        print(f"[!] {len(missing)} 项指标没有值（阶段未跑或子项失败）：{', '.join(missing)}",
              file=sys.stderr)
    batch = result.get("batch")
    if batch:
        if batch["sample_count"] < batch["repeat"]:
            print(f"[!] 批次只有 {batch['sample_count']}/{batch['repeat']} 个样本；"
                  f"缺失 run: {', '.join(batch['missing_run_ids']) or '—'}", file=sys.stderr)
        if batch["status"] != "ok":
            print(f"[!] 批次状态={batch['status']}；指标只使用已回收样本的中位数",
                  file=sys.stderr)
        short = sorted(
            key for key, detail in result.get("samples", {}).items()
            if detail.get("n", 0) < batch["repeat"]
        )
        if short:
            shown = ", ".join(short[:8]) + (" ..." if len(short) > 8 else "")
            print(f"[!] {len(short)} 项指标样本数少于 {batch['repeat']}：{shown}",
                  file=sys.stderr)
    if result.get("stages_partial"):
        print(f"[!] 部分完成的阶段：{', '.join(result['stages_partial'])}", file=sys.stderr)
    if result["stages_missing"]:
        print(f"[!] 未成功的阶段：{', '.join(result['stages_missing'])}", file=sys.stderr)
    stages_seen = set(result["stages_done"]) | set(result.get("stages_partial", []))
    if result.get("steal_samples", 0) == 0 and "stress" in stages_seen:
        print("[!] stress 跑过但没有解析到 mpstat 样本：%steal 会按未测处理，"
              "请检查 mpstat.log", file=sys.stderr)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"[OK] 已写出 {args.output}")
    elif args.submit:
        m, mt = result["machine"], result["metrics"]
        suffix = ""
        if batch:
            suffix = f"  批次 {batch['id']} ({batch['sample_count']}/{batch['repeat']} 样本)"
        print(f"[i] {m['id']}  {m['vendor']} {m['spec']}  "
              f"单核 {mt['cpu_single_eps']:.0f} eps  "
              f"4K读 {mt['disk_4k_read_iops']:.0f} IOPS  "
              f"p99 {mt['disk_4k_read_p99_ms']:.2f}ms{suffix}")
    else:
        print(text)

    if args.submit:
        key = args.key or os.environ.get("BENCH_API_KEY", "")
        if not key:
            raise SystemExit("[ERR] 缺少提交密钥：设 BENCH_API_KEY 环境变量，或用 --key")
        code, body = post_result(args.submit, key, result)
        if code in (200, 201):
            print(f"[OK] 已提交到 {args.submit}：{body.strip()}")
        elif code == -1:
            print(f"[ERR] 提交失败：{body}", file=sys.stderr)
            return 1
        else:
            print(f"[ERR] 提交失败 HTTP {code}: {body.strip()[:300]}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
