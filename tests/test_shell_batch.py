#!/usr/bin/env python3
"""run --repeat / status/collect/report --batch 的离线端到端回归。"""

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cloud-benchmark.sh"

FAKE_SSH = r"""#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-i|-F|-l) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] && shift
exec bash -c "$*"
"""

# 测试环境不依赖 tmux；把 new-session 直接转成 setsid 后台进程。
FAKE_TMUX = r"""#!/bin/bash
case "$1" in
  has-session) exit 1 ;;
  kill-session) exit 0 ;;
  new-session)
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -d) shift ;;
        -s) shift 2 ;;
        *) break ;;
      esac
    done
    [ $# -gt 0 ] || exit 1
    setsid bash -c "$*" >/dev/null 2>&1 </dev/null &
    exit 0
    ;;
  *) exit 0 ;;
esac
"""


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.results = root / "results"
        self.bin = root / "bin"
        self.home.mkdir()
        self.results.mkdir()
        self.bin.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("tmux", FAKE_TMUX)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        (self.results / "hosts.tsv").write_text(
            "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\tregion\tvendor_type\n"
            "demo\tfakehost\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\t空机\t广州\tlight\n",
            encoding="utf-8",
        )
        self.remote = self.home / "cloud-benchmark"

    def env(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CB_RESULTS_DIR"] = str(self.results)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return env

    def run_cli(self, *args, timeout=40):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env(), check=False, timeout=timeout,
            capture_output=True, text=True,
        )

    def wait_batch(self, batch_id, timeout=20):
        runs = self.remote / "runs"
        deadline = time.time() + timeout
        while time.time() < deadline:
            statuses = []
            for out in sorted(runs.glob(f"{batch_id}-r*/out")):
                status_file = out / "run-status.tsv"
                if not status_file.is_file():
                    statuses.append("starting")
                    continue
                text = status_file.read_text(encoding="utf-8")
                m = re.search(r"^status\t(.+)$", text, re.M)
                statuses.append(m.group(1) if m else "starting")
            if len(statuses) == 2 and all(s not in ("running", "starting") for s in statuses):
                return statuses
            time.sleep(0.1)
        self.fail(f"批次 {batch_id} 超时未完成；状态={statuses}")

    def wait_run(self, run_id, timeout=20):
        status_file = self.remote / "runs" / run_id / "out" / "run-status.tsv"
        deadline = time.time() + timeout
        status = "starting"
        while time.time() < deadline:
            if status_file.is_file():
                text = status_file.read_text(encoding="utf-8")
                m = re.search(r"^status\t(.+)$", text, re.M)
                status = m.group(1) if m else "starting"
                if status not in ("running", "starting"):
                    return status
            time.sleep(0.1)
        self.fail(f"单次 run {run_id} 超时未完成；状态={status}")

    def test_repeat_one_keeps_single_run_behavior(self):
        proc = self.run_cli("run", "--name", "demo", "--stages", "env", "--repeat", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        m = re.search(r"运行 ID: ([A-Za-z0-9._-]+)", proc.stdout)
        self.assertIsNotNone(m, proc.stdout)
        run_id = m.group(1)
        self.assertEqual(self.wait_run(run_id), "ok")
        self.assertEqual((self.remote / "latest-run").read_text(encoding="utf-8").strip(), run_id)
        self.assertFalse((self.remote / "latest-batch").exists())
        self.assertEqual(len(list((self.remote / "runs").glob("*/out"))), 1)

    def test_repeat_collect_report(self):
        proc = self.run_cli("run", "--name", "demo", "--stages", "env", "--repeat", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        m = re.search(r"批次 ID: ([A-Za-z0-9._-]+)", proc.stdout)
        self.assertIsNotNone(m, proc.stdout)
        batch_id = m.group(1)

        statuses = self.wait_batch(batch_id)
        self.assertEqual(statuses, ["ok", "ok"])
        self.assertEqual((self.remote / "latest-batch").read_text(encoding="utf-8").strip(), batch_id)
        manifest = (self.remote / "batches" / batch_id / "batch.tsv").read_text(encoding="utf-8")
        run_ids = re.search(r"^run_ids\t(.+)$", manifest, re.M).group(1).split()
        self.assertEqual(run_ids, [f"{batch_id}-r01", f"{batch_id}-r02"])
        self.assertEqual((self.remote / "latest-run").read_text(encoding="utf-8").strip(), run_ids[-1])

        proc = self.run_cli("status", "--name", "demo", "--batch", batch_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(run_ids[0], proc.stdout)
        self.assertIn(run_ids[1], proc.stdout)

        for i, out in enumerate(sorted((self.remote / "runs").glob(f"{batch_id}-r*/out"))):
            (out / "metrics.tsv").write_text(
                f"fio_4k_randread_iops\t{1000 + i * 100}\tIOPS\n"
                f"fio_4k_randread_p99_ms\t{10 + i}\tms\n"
                f"sysbench_cpu_single_eps\t{400 + i}\tevents/s\n"
                "stress_avg_steal_pct\t0.0\t%\n"
                "stress_samples\t60\t\n",
                encoding="utf-8",
            )

        proc = self.run_cli("collect", "--name", "demo", "--batch", batch_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for run_id in run_ids:
            self.assertTrue((self.results / "demo" / run_id / "system-info.txt").is_file())
        self.assertTrue((self.results / "demo" / "batches" / f"{batch_id}.tsv").is_file())
        self.assertEqual(
            (self.results / "demo" / "LATEST").read_text(encoding="utf-8").strip(),
            run_ids[-1],
        )

        proc = self.run_cli("report", "--name", "demo", "--batch", batch_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2/2", proc.stdout)
        self.assertIn(batch_id, proc.stdout)
        self.assertIn("n=2", proc.stdout)
        self.assertIn("1,050", proc.stdout)
        self.assertIn("p95 1,100", proc.stdout)

        proc = self.run_cli("submit", "--name", "demo", "--batch", batch_id, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"batch"', proc.stdout)
        self.assertIn('"samples"', proc.stdout)
        self.assertIn('"disk_4k_read_iops": 1050', proc.stdout)

        proc = self.run_cli("summarize", "--batch", batch_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已生成对比表", proc.stdout)


class BatchCollectGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.results = root / "results"
        self.bin = root / "bin"
        self.home.mkdir()
        self.results.mkdir()
        self.bin.mkdir()
        path = self.bin / "ssh"
        path.write_text(FAKE_SSH, encoding="utf-8")
        path.chmod(0o755)
        (self.results / "hosts.tsv").write_text(
            "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\n"
            "demo\tfakehost\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\n",
            encoding="utf-8",
        )
        base = self.home / "cloud-benchmark"
        (base / "batches" / "b1").mkdir(parents=True)
        (base / "batches" / "b1" / "batch.tsv").write_text(
            "batch_id\tb1\nname\tdemo\nprofile\tquick\nrepeat\t2\n"
            "stages\tenv\ncreated_at\t2026-11-06T00:00:00Z\n"
            "run_ids\tb1-r01 b1-r02\n",
            encoding="utf-8",
        )
        for run_id, status in (("b1-r01", "ok"), ("b1-r02", "running")):
            out = base / "runs" / run_id / "out"
            out.mkdir(parents=True)
            (out / "run-status.tsv").write_text(
                f"run_id\t{run_id}\nprofile\tquick\nstatus\t{status}\n"
                "started_at\t2026-11-06T00:00:00Z\nfinished_at\t\nstages_requested\tenv\n",
                encoding="utf-8",
            )

    def env(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CB_RESULTS_DIR"] = str(self.results)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return env

    def test_running_sample_refused_without_force(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "collect", "--name", "demo", "--batch", "b1"],
            env=self.env(), check=False, timeout=20, capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未完成", proc.stderr)
        self.assertFalse((self.results / "demo" / "b1-r01").exists())


if __name__ == "__main__":
    unittest.main()
