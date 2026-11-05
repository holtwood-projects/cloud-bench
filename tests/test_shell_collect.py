#!/usr/bin/env python3
"""collect 状态机回归：starting/running 拒绝，旧 run 不倒退 LATEST。"""

import os
import subprocess
import tempfile
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


class CollectStateTest(unittest.TestCase):
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
        fake = self.bin / "ssh"
        fake.write_text(FAKE_SSH, encoding="utf-8")
        fake.chmod(0o755)
        (self.results / "hosts.tsv").write_text(
            "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\tregion\tvendor_type\n"
            "demo\tfakehost\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\t空机\t广州\tlight\n",
            encoding="utf-8",
        )
        base = self.home / "cloud-benchmark"
        self.runs = base / "runs"
        for run_id, status in (("old", "ok"), ("new", "ok"), ("running", "running")):
            out = self.runs / run_id / "out"
            out.mkdir(parents=True)
            (out / "run-status.tsv").write_text(
                f"run_id\t{run_id}\nprofile\tquick\nstatus\t{status}\n"
                "started_at\t2026-11-05T10:00:00Z\nfinished_at\t\nstages_requested\tenv\n",
                encoding="utf-8",
            )
            (out / "stage-status.tsv").write_text(
                "stage\tstatus\tstarted_at\tfinished_at\n"
                "env\tok\t2026-11-05T10:00:00Z\t2026-11-05T10:00:01Z\n",
                encoding="utf-8",
            )
        # 只有目录、没有 run-status：模拟 __exec 尚未开始（含 --at 等待）。
        (self.runs / "starting" / "out").mkdir(parents=True)
        (base / "latest-run").write_text("new\n", encoding="utf-8")

    def collect(self, *args):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CB_RESULTS_DIR"] = str(self.results)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            ["bash", str(SCRIPT), "collect", "--name", "demo", *args],
            env=env, check=False, timeout=20,
            capture_output=True, text=True,
        )

    def test_starting_is_refused(self):
        proc = self.collect("--run", "starting")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("starting", proc.stderr)

    def test_running_is_refused(self):
        proc = self.collect("--run", "running")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("running", proc.stderr)

    def test_old_collect_does_not_rewind_latest(self):
        proc = self.collect("--run", "old")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.results / "demo" / "LATEST").exists())
        proc = self.collect()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (self.results / "demo" / "LATEST").read_text(encoding="utf-8"), "new\n"
        )


if __name__ == "__main__":
    unittest.main()
