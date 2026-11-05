#!/usr/bin/env python3
"""PTS 阶段状态回归：完整成绩 -> ok；只跑出一部分 -> partial。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cloud-benchmark.sh"

FAKE_PTS = r"""#!/bin/sh
case "$1" in
  batch-setup) cat >/dev/null; exit 0 ;;
  batch-list-available-tests) exit 0 ;;
  batch-benchmark)
    read -r _ || true
    %s
    exit 0
    ;;
  *) exit 0 ;;
esac
"""

ONLY_7ZIP = r"""
    echo 'Compression Rating: 7-Zip 24.09'
    echo '    Average: 12345 MIPS'
    echo 'Decompression Rating: 7-Zip 24.09'
    echo '    Average: 23456 MIPS'
"""

ALL_RESULTS = ONLY_7ZIP + r"""
    echo 'SET - Parallel Connections: 50:'
    echo '    Average: 100000 Requests Per Second'
"""


class PtsStageTest(unittest.TestCase):
    def run_pts(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bin_dir = root / "bin"
        out = root / "out"
        home = root / "home"
        bin_dir.mkdir()
        home.mkdir()
        fake = bin_dir / "phoronix-test-suite"
        fake.write_text(FAKE_PTS % body, encoding="utf-8")
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["HOME"] = str(home)
        env["CB_PTS_TESTS"] = "compress-7zip redis"
        subprocess.run(
            ["bash", str(SCRIPT), "__exec", "--out", str(out), "--stages", "pts"],
            env=env, check=False, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        status = (out / "stage-status.tsv").read_text(encoding="utf-8")
        return out, status.splitlines()[-1].split("\t")[1]

    def test_pts_partial_when_requested_test_missing(self):
        out, status = self.run_pts(ONLY_7ZIP)
        self.assertEqual(status, "partial")
        self.assertIn("pts", (out / "partial-stages.txt").read_text(encoding="utf-8"))

    def test_pts_ok_when_all_parsed_tests_present(self):
        out, status = self.run_pts(ALL_RESULTS)
        self.assertEqual(status, "ok")
        self.assertTrue((out / "DONE").is_file())
        self.assertIn("pts_redis_rps", (out / "metrics.tsv").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
