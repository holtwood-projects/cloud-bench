#!/usr/bin/env python3
"""共享解析层回归测试（零第三方依赖）。"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bench_metrics as bm  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "cloud_bench_summarize", ROOT / "scripts" / "cloud-benchmark-summarize.py"
)
summarize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summarize)


class BenchMetricsTest(unittest.TestCase):
    def test_fio_p99_and_p999(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "fio-4k-randread.json").write_text(json.dumps({
                "jobs": [{
                    "job options": {"size": "2G", "numjobs": "4"},
                    "read": {
                        "iops": 10000,
                        "bw": 40000,
                        "clat_ns": {"percentile": {
                            "99.000000": 1_234_567,
                            "99.900000": 2_345_678,
                        }},
                    },
                }]
            }), encoding="utf-8")
            m = bm.parse_fio(d)
            self.assertAlmostEqual(m["fio_4k_randread_iops"], 10000)
            self.assertAlmostEqual(m["fio_4k_randread_p99_ms"], 1.234567)
            self.assertAlmostEqual(m["fio_4k_randread_p999_ms"], 2.345678)

    def test_parse_stress_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "mpstat.log").write_text(
                "Linux 6.8.0-demo (host)  11/05/2026  _x86_64_  (2 CPU)\n"
                "\n"
                "10:00:01 AM CPU %usr %nice %sys %iowait %irq %soft %steal %guest %gnice %idle\n"
                "10:00:02 AM all 0.50 0.00 0.50 0.00 0.00 0.00 0.00 0.00 0.00 99.00\n"
                "10:00:03 AM all 0.50 0.00 0.50 0.00 0.00 0.00 0.25 0.00 0.00 98.75\n",
                encoding="utf-8",
            )
            m = bm.parse_stress(d)
            self.assertEqual(m["stress_samples"], 2)
            self.assertAlmostEqual(m["stress_avg_steal_pct"], 0.125)
            self.assertAlmostEqual(m["stress_max_steal_pct"], 0.25)

    def test_avx512_explicit_no_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "system-info.txt").write_text(
                "Model name: Demo\nCPU(s): 2\n"
                "Thread(s) per core: 2\nCore(s) per socket: 1\nSocket(s): 1\n"
                "--- 关键指令集判定 ---\navx512f 无\n",
                encoding="utf-8",
            )
            info = bm.read_sysinfo(d)
            self.assertFalse(info["avx512"])
            self.assertEqual(info["physical_cores"], 1)
            self.assertEqual(info["threads"], 2)

    def test_avx512_flags_fallback_is_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "system-info.txt").write_text(
                "flags\t\t: sse4_2 avx2 avx512f\n", encoding="utf-8"
            )
            self.assertTrue(bm.read_sysinfo(d)["avx512"])

    def test_sysinfo_explicit_loadavg_and_physical_cores(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "system-info.txt").write_text(
                "Model name: Demo\nCPU(s): 4\n"
                "Thread(s) per core: 2\nCore(s) per socket: 2\nSocket(s): 2\n"
                "loadavg: 0.10 0.20 0.30 1/100 123\n"
                "物理核数(去重 Core,Socket)=3\n",
                encoding="utf-8",
            )
            info = bm.read_sysinfo(d)
            self.assertEqual(info["loadavg"], "0.10 0.20 0.30 1/100 123")
            self.assertEqual(info["physical_cores"], 3)

    def test_hosts_header_and_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp) / "hosts.tsv"
            hosts.write_text(
                "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\tregion\tvendor_type\n"
                "demo\tx\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\t空机\t广州\tlight\n",
                encoding="utf-8",
            )
            rows = bm.read_hosts(hosts)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "demo")
            self.assertEqual(rows[0]["region"], "广州")
            self.assertEqual(rows[0]["vendor_type"], "light")

    def test_resolve_run_dir_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                bm.resolve_run_dir(tmp, "demo", "../escape")
            with self.assertRaises(SystemExit):
                bm.resolve_run_dir(tmp, "demo", "bad;id")

    def test_latest_run_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "demo"
            (base / "r1").mkdir(parents=True)
            (base / "r2").mkdir()
            (base / "LATEST").write_text("r2\n", encoding="utf-8")
            self.assertEqual(Path(bm.resolve_run_dir(tmp, "demo")), base / "r2")
            self.assertEqual(Path(bm.resolve_run_dir(tmp, "demo", "r1")), base / "r1")

    def test_raw_fio_beats_stale_metrics_and_stage_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "r1"
            d = Path(tmp) / "demo" / rid
            d.mkdir(parents=True)
            (Path(tmp) / "demo" / "LATEST").write_text(rid + "\n", encoding="utf-8")
            hosts = Path(tmp) / "hosts.tsv"
            hosts.write_text(
                "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\tregion\tvendor_type\n"
                "demo\tx\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99.5\t首年\t空机\t广州\tlight\n",
                encoding="utf-8",
            )
            (d / "system-info.txt").write_text(
                "Model name: Demo\nCPU(s): 2\n"
                "Thread(s) per core: 2\nCore(s) per socket: 1\nSocket(s): 1\n"
                "--- 关键指令集判定 ---\navx512f 无\nMem: 1.6Gi 1.6Gi 0B 0B\n",
                encoding="utf-8",
            )
            (d / "run-status.tsv").write_text(
                "run_id\tr1\nprofile\tquick\nstatus\tpartial\n"
                "started_at\t2026-11-05T12:00:00+08:00\n"
                "finished_at\t2026-11-05T12:16:00+08:00\n"
                "stages_requested\tenv fio stress\n",
                encoding="utf-8",
            )
            (d / "stage-status.tsv").write_text(
                "stage\tstatus\tstarted_at\tfinished_at\n"
                "fio\tok\t2026-11-05T12:00:00+08:00\t2026-11-05T12:05:00+08:00\n"
                "stress\tfailed\t2026-11-05T12:05:00+08:00\t2026-11-05T12:10:00+08:00\n",
                encoding="utf-8",
            )
            (d / "metrics.tsv").write_text(
                "fio_4k_randread_iops\t123\tIOPS\nsysbench_cpu_single_eps\t999\tevents/s\n",
                encoding="utf-8",
            )
            (d / "fio-4k-randread.json").write_text(json.dumps({
                "jobs": [{
                    "job options": {"size": "2G", "numjobs": "4"},
                    "read": {
                        "iops": 10000,
                        "bw": 40000,
                        "clat_ns": {"percentile": {
                            "99.000000": 1_234_567,
                            "99.900000": 2_345_678,
                        }},
                    },
                }]
            }), encoding="utf-8")

            result = bm.build_result(tmp, bm.hosts_by_name(hosts), "demo", run_id=rid)
            self.assertEqual(result["machine"]["region"], "广州")
            self.assertFalse(result["machine"]["avx512"])
            self.assertEqual(result["machine"]["physical_cores"], 1)
            self.assertEqual(result["machine"]["fio_total_size_gb"], 8)
            self.assertEqual(result["metrics"]["disk_4k_read_iops"], 10000)  # 原始产物优先
            # 原始产物缺失时才回落到 metrics.tsv；新运行目录会在开始时清空它。
            self.assertEqual(result["metrics"]["cpu_single_eps"], 999)
            self.assertAlmostEqual(result["metrics"]["disk_4k_read_p999_ms"], 2.345678)
            self.assertEqual(result["stages_done"], ["fio"])
            self.assertEqual(result["stages_partial"], [])
            self.assertEqual(set(result["stages_missing"]), {"env", "stress"})

    def test_parse_error_warning_goes_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "fio-4k-randread.json").write_text(
                '{"jobs":[{"read":"oops"}]}', encoding="utf-8"
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                bm.collect_metrics(d)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("解析异常", stderr.getvalue())

    def test_legacy_us_metrics_are_converted_to_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "metrics.tsv").write_text(
                "fio_4k_randread_p99_us\t1000\tus\n"
                "fio_4k_randread_p999_us\t2000\tus\n",
                encoding="utf-8",
            )
            m, _local, _remote = bm.collect_metrics(d)
            self.assertAlmostEqual(m["fio_4k_randread_p99_ms"], 1.0)
            self.assertAlmostEqual(m["fio_4k_randread_p999_ms"], 2.0)

    def test_build_result_schema_and_missing_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            run_id = "20261105T120000Z-abc123"
            d = results / "demo" / run_id
            d.mkdir(parents=True)
            (results / "demo" / "LATEST").write_text(run_id + "\n", encoding="utf-8")
            (d / "run-status.tsv").write_text(
                "run_id\t" + run_id + "\n"
                "profile\tquick\n"
                "status\tpartial\n"
                "started_at\t2026-11-05T12:00:00Z\n"
                "finished_at\t2026-11-05T12:16:00Z\n"
                "stages_requested\tenv fio stress pts\n",
                encoding="utf-8",
            )
            (d / "stage-status.tsv").write_text(
                "stage\tstatus\tstarted_at\tfinished_at\n"
                "env\tok\t2026-11-05T12:00:00Z\t2026-11-05T12:00:10Z\n"
                "fio\tfailed\t2026-11-05T12:00:10Z\t2026-11-05T12:08:00Z\n"
                "stress\tok\t2026-11-05T12:08:00Z\t2026-11-05T12:13:00Z\n"
                "pts\tpartial\t2026-11-05T12:13:00Z\t2026-11-05T12:16:00Z\n",
                encoding="utf-8",
            )
            (d / "mpstat.log").write_text(
                "10:00:01 AM CPU %usr %nice %sys %iowait %irq %soft %steal %guest %gnice %idle\n"
                "10:00:02 AM all 0.50 0.00 0.50 0.00 0.00 0.00 0.00 0.00 0.00 99.00\n"
                "10:00:03 AM all 0.50 0.00 0.50 0.00 0.00 0.00 0.25 0.00 0.00 98.75\n",
                encoding="utf-8",
            )
            (d / "system-info.txt").write_text(
                "Model name: Demo CPU\nCPU(s): 2\n"
                "Thread(s) per core: 2\nCore(s) per socket: 1\nSocket(s): 1\n"
                "kernel: 6.8.0-demo\n",
                encoding="utf-8",
            )
            (d / "metrics.tsv").write_text(
                "sysbench_cpu_single_eps\t123\tevents/s\n", encoding="utf-8"
            )
            hosts = {
                "demo": {
                    "name": "demo", "vendor": "示例云", "vendor_type": "light",
                    "spec": "2C2G", "vcpu": "2", "ram_gb": "2", "bandwidth": "1M",
                    "price_cny": "99", "price_note": "首年", "region": "广州",
                    "role": "空机",
                }
            }
            result = bm.build_result(results, hosts, "demo")
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["method_version"], bm.TOOL_VERSION)
            self.assertEqual(result["run"]["id"], run_id)
            self.assertEqual(result["run"]["status"], "partial")
            self.assertEqual(result["stages_done"], ["env", "stress"])
            self.assertEqual(result["stages_partial"], ["pts"])
            self.assertEqual(result["stages_missing"], ["fio"])
            self.assertEqual(result["metrics"]["cpu_single_eps"], 123)
            self.assertAlmostEqual(result["metrics"]["steal_avg_pct"], 0.125)
            self.assertEqual(result["steal_samples"], 2)
            self.assertEqual(result["machine"]["region"], "广州")

    def test_report_does_not_invent_conclusions_for_missing_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "demo"
            d.mkdir()
            (root / "hosts.tsv").write_text(
                "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\n"
                "demo\tx\t1\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\t空机\n",
                encoding="utf-8",
            )
            (d / "metrics.tsv").write_text(
                "fio_4k_randread_iops\t10000\tIOPS\n"
                "sysbench_cpu_single_eps\t500\tevents/s\n",
                encoding="utf-8",
            )
            old_results = summarize.RESULTS
            old_hosts = summarize.HOSTS
            summarize.RESULTS = tmp
            summarize.HOSTS = str(root / "hosts.tsv")
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    summarize.render_report("demo")
                text = buf.getvalue()
            finally:
                summarize.RESULTS = old_results
                summarize.HOSTS = old_hosts
            self.assertIn("不能判断卡顿", text)
            self.assertIn("不能判断是否超售", text)
            self.assertNotIn("基本流畅", text)
            self.assertNotIn("存在资源争抢", text)

    def test_legacy_stage_progress_limits_missing_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "progress.log").write_text(
                "[x] >>> 阶段开始: env\n[x] <<< 阶段结束: env\n"
                "[x] >>> 阶段开始: fio\n[x] <<< 阶段结束: fio\n",
                encoding="utf-8",
            )
            (d / "system-info.txt").write_text("x\n", encoding="utf-8")
            (d / "fio.log").write_text("x\n", encoding="utf-8")
            (d / "failed-stages.txt").write_text("fio\n", encoding="utf-8")
            done, partial, missing = bm.read_stages_done(d)
            self.assertEqual(done, ["env"])
            self.assertEqual(partial, [])
            self.assertEqual(missing, ["fio"])

    def test_speedtest_cli_simple_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "speedtest.txt").write_text(
                "Ping: 12.3 ms\nDownload: 88.8 Mbit/s\nUpload: 9.9 Mbit/s\n",
                encoding="utf-8",
            )
            m = bm.parse_speedtest(d)
            self.assertAlmostEqual(m["speedtest_down_mbps"], 88.8)
            self.assertAlmostEqual(m["speedtest_up_mbps"], 9.9)
            self.assertAlmostEqual(m["speedtest_latency_ms"], 12.3)

    def test_unknown_metrics_do_not_produce_capability_claims(self):
        can, warn, cannot = summarize._what_can_it_run(
            2, ("未测", ""), ("未测", ""), ("未测", ""), ("未测", "")
        )
        self.assertNotIn("轻量数据库（MySQL / PostgreSQL / Redis）", can)
        self.assertNotIn("5~10 个轻量容器（面板 / Nginx / 小服务）", can)
        self.assertTrue(any("不能判断" in x for x in warn))


class BatchAggregationTest(unittest.TestCase):
    def _hosts(self, root):
        hosts = root / "hosts.tsv"
        hosts.write_text(
            "name\tssh_alias\tip\tuser\tport\tvendor\tspec\tvcpu\tram_gb\tbandwidth\tprice_cny\tprice_note\trole\tregion\tvendor_type\n"
            "demo\tx\t1.2.3.4\troot\t22\t示例云\t2C2G\t2\t2\t1M\t99\t首年\t空机\t广州\tlight\n",
            encoding="utf-8",
        )
        return bm.hosts_by_name(hosts)

    def _batch_fixture(self, root):
        base = root / "demo"
        (base / "batches").mkdir(parents=True)
        run_ids = ["b-r01", "b-r02", "b-r03"]
        (base / "batches" / "b.tsv").write_text(
            "batch_id\tb\nname\tdemo\nprofile\tquick\nrepeat\t3\n"
            "stages\tenv fio stress\ncreated_at\t2026-11-06T00:00:00Z\n"
            "run_ids\t" + " ".join(run_ids) + "\n",
            encoding="utf-8",
        )

        def add_run(run_id, status, stages, metrics):
            d = base / run_id
            d.mkdir()
            (d / "run-status.tsv").write_text(
                f"run_id\t{run_id}\nprofile\tquick\nstatus\t{status}\n"
                "started_at\t2026-11-06T00:00:00Z\n"
                "finished_at\t2026-11-06T00:01:00Z\n"
                "stages_requested\tenv fio stress\n",
                encoding="utf-8",
            )
            (d / "stage-status.tsv").write_text(
                "stage\tstatus\tstarted_at\tfinished_at\n"
                + "".join(f"{stage}\t{state}\tx\ty\n" for stage, state in stages.items()),
                encoding="utf-8",
            )
            (d / "metrics.tsv").write_text(
                "".join(f"{key}\t{value}\t\n" for key, value in metrics.items()),
                encoding="utf-8",
            )
            return d

        add_run("b-r01", "ok", {"env": "ok", "fio": "ok", "stress": "ok"}, {
            "fio_4k_randread_iops": 100,
            "sysbench_cpu_single_eps": 400,
            "stress_avg_steal_pct": 0,
            "stress_samples": 60,
        })
        d2 = add_run("b-r02", "partial", {"env": "ok", "fio": "partial", "stress": "failed"}, {
            "fio_4k_randread_iops": 200,
            "sysbench_cpu_single_eps": 402,
        })
        (d2 / "system-info.txt").write_text(
            "Model name: Demo CPU\nCPU(s): 2\n"
            "Thread(s) per core: 1\nCore(s) per socket: 2\nSocket(s): 1\n",
            encoding="utf-8",
        )
        return base, run_ids

    def test_aggregate_metrics_nearest_rank_and_missing(self):
        samples = bm.aggregate_metrics([
            {"iops": 100, "missing": None, "text": "x", "flag": True},
            {"iops": 200},
            {"iops": 300},
        ])
        self.assertEqual(samples["iops"]["n"], 3)
        self.assertEqual(samples["iops"]["median"], 200)
        self.assertEqual(samples["iops"]["p95"], 300)
        self.assertEqual(samples["iops"]["min"], 100)
        self.assertEqual(samples["iops"]["max"], 300)
        self.assertNotIn("missing", samples)
        self.assertNotIn("text", samples)
        self.assertNotIn("flag", samples)
        self.assertEqual(bm.aggregate_metrics([{"iops": 10}])["iops"]["p95"], 10)
        self.assertEqual(bm.aggregate_metrics([{"iops": 10}, {"iops": 20}])["iops"]["p95"], 20)

    def test_build_batch_result_partial_and_missing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hosts = self._hosts(root)
            _base, run_ids = self._batch_fixture(root)
            result = bm.build_batch_result(root, hosts, "demo", "b")

            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["run"]["id"], "b")
            self.assertEqual(result["run"]["status"], "partial")
            self.assertEqual(result["batch"]["sample_count"], 2)
            self.assertEqual(result["batch"]["repeat"], 3)
            self.assertEqual(result["batch"]["ok_run_count"], 1)
            self.assertEqual(result["batch"]["missing_run_ids"], [run_ids[2]])
            self.assertEqual([r["collected"] for r in result["batch"]["runs"]], [True, True, False])
            self.assertEqual(result["samples"]["fio_4k_randread_iops"]["n"], 2)
            self.assertEqual(result["samples"]["fio_4k_randread_iops"]["median"], 150)
            self.assertEqual(result["metrics"]["disk_4k_read_iops"], 150)
            self.assertEqual(result["machine"]["cpu_model"], "Demo CPU")
            self.assertEqual(result["machine"]["physical_cores"], 2)
            self.assertEqual(result["steal_samples"], 60)
            self.assertEqual(set(result["stages_partial"]), {"env", "fio", "stress"})
            self.assertIn("disk_4k_read_p99_ms", result["metrics_missing"])

    def test_batch_report_marks_missing_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._hosts(root)
            _base, run_ids = self._batch_fixture(root)
            old_results = summarize.RESULTS
            old_hosts = summarize.HOSTS
            summarize.RESULTS = str(root)
            summarize.HOSTS = str(root / "hosts.tsv")
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    summarize.render_batch_report("demo", "b")
                text = buf.getvalue()
            finally:
                summarize.RESULTS = old_results
                summarize.HOSTS = old_hosts
            self.assertIn("2/3 个样本", text)
            self.assertIn("样本数不足", text)
            self.assertIn(run_ids[2], text)
            self.assertIn("n=2", text)

    def test_single_result_stays_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "results"
            run_id = "20261106T000000Z-abc123"
            d = root / "demo" / run_id
            d.mkdir(parents=True)
            (root / "demo" / "LATEST").write_text(run_id + "\n", encoding="utf-8")
            (d / "run-status.tsv").write_text(
                f"run_id\t{run_id}\nprofile\tquick\nstatus\tok\n"
                "started_at\t2026-11-06T00:00:00Z\nfinished_at\t2026-11-06T00:01:00Z\n"
                "stages_requested\tenv\n",
                encoding="utf-8",
            )
            (d / "stage-status.tsv").write_text(
                "stage\tstatus\tstarted_at\tfinished_at\nenv\tok\tx\ty\n",
                encoding="utf-8",
            )
            hosts = self._hosts(root)
            result = bm.build_result(root, hosts, "demo")
            self.assertNotIn("batch", result)
            self.assertNotIn("samples", result)
            self.assertEqual(result["schema_version"], 2)

    def test_resolve_batch_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                bm.resolve_batch_runs(tmp, "demo", "../escape")


if __name__ == "__main__":
    unittest.main()
