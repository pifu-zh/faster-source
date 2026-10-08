import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from select_mirror import (
    Config,
    ConfigError,
    CurlSession,
    Mirror,
    ProbeConfig,
    ProbeResult,
    build_curl_command,
    load_config,
    main,
    make_curl_runner,
    parse_write_out,
    probe_all,
    probe_url,
    rewrite_sources,
    select_winner,
)


SAMPLE = """
# candidates
suite: jammy
probe:
  connect_timeout_sec: 1
  max_time_sec: 2
  deadline_sec: 5
mirrors:
  - name: aliyun
    baseurl: https://mirrors.aliyun.com/ubuntu/
  - name: official
    baseurl: http://archive.ubuntu.com/ubuntu
"""


class LoadConfigTest(unittest.TestCase):
    def test_parses_sample_and_strips_slash(self):
        config = load_config(SAMPLE)
        self.assertEqual(config.suite, "jammy")
        self.assertEqual(config.probe.connect_timeout_sec, 1)
        self.assertEqual(config.probe.max_time_sec, 2)
        self.assertEqual(config.probe.deadline_sec, 5)
        self.assertEqual(config.mirrors[0].name, "aliyun")
        self.assertEqual(config.mirrors[0].baseurl, "https://mirrors.aliyun.com/ubuntu")
        self.assertEqual(
            probe_url(config.mirrors[0], config.suite),
            "https://mirrors.aliyun.com/ubuntu/dists/jammy/InRelease",
        )

    def test_shipped_file_has_seven_mirrors(self):
        text = Path("mirrors.yaml").read_text(encoding="utf-8")
        config = load_config(text)
        self.assertEqual(
            [mirror.name for mirror in config.mirrors],
            ["aliyun", "huaweicloud", "tencent", "bfsu", "ustc", "tuna", "official"],
        )

    def test_missing_baseurl_is_rejected(self):
        text = SAMPLE.replace("    baseurl: http://archive.ubuntu.com/ubuntu\n", "")
        with self.assertRaises(ConfigError):
            load_config(text)

    def test_duplicate_name_is_rejected(self):
        text = SAMPLE.replace("name: official", "name: aliyun")
        with self.assertRaises(ConfigError):
            load_config(text)

    def test_timeout_order_is_rejected(self):
        text = SAMPLE.replace("max_time_sec: 2", "max_time_sec: 9")
        with self.assertRaises(ConfigError):
            load_config(text)


def _result(name, baseurl, time_total, http_code=200, returncode=0, body="Codename: jammy\n"):
    return ProbeResult(
        mirror=Mirror(name=name, baseurl=baseurl),
        http_code=http_code,
        time_total=time_total,
        body=body,
        returncode=returncode,
    )


class SelectWinnerTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(SAMPLE)

    def test_lowest_time_wins(self):
        winner = select_winner(
            [
                _result("official", "http://archive.ubuntu.com/ubuntu", 1.4),
                _result("aliyun", "https://mirrors.aliyun.com/ubuntu", 0.12),
            ],
            self.config,
        )
        self.assertEqual(winner.mirror.name, "aliyun")

    def test_tie_keeps_yaml_order(self):
        winner = select_winner(
            [
                _result("official", "http://archive.ubuntu.com/ubuntu", 0.2),
                _result("aliyun", "https://mirrors.aliyun.com/ubuntu", 0.2),
            ],
            self.config,
        )
        self.assertEqual(winner.mirror.name, "aliyun")

    def test_discards_failed_probes(self):
        results = [
            _result("aliyun", "https://mirrors.aliyun.com/ubuntu", 0.1, http_code=404),
            _result("aliyun", "https://mirrors.aliyun.com/ubuntu", 0.1, body="not a release\n"),
            _result("aliyun", "https://mirrors.aliyun.com/ubuntu", 0.1, returncode=28),
            _result("official", "http://archive.ubuntu.com/ubuntu", 0.4, body="note Codename: jammy inside\n"),
        ]
        self.assertIsNone(select_winner(results, self.config))


SOURCES = """\
deb http://archive.ubuntu.com/ubuntu/ jammy main restricted
deb http://security.ubuntu.com/ubuntu/ jammy-security main restricted
deb-src http://archive.ubuntu.com/ubuntu/ jammy universe
# deb-src http://archive.ubuntu.com/ubuntu/ jammy main restricted
deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /
deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /
deb https://mirrors.ustc.edu.cn/ubuntu/ jammy multiverse
"""


class RewriteSourcesTest(unittest.TestCase):
    def test_replaces_archive_security_and_known_mirror(self):
        mirrors = (
            Mirror("aliyun", "https://mirrors.aliyun.com/ubuntu"),
            Mirror("ustc", "https://mirrors.ustc.edu.cn/ubuntu"),
            Mirror("official", "http://archive.ubuntu.com/ubuntu"),
        )
        winner = mirrors[0]
        text, changed = rewrite_sources(SOURCES, mirrors, winner)
        self.assertTrue(changed)
        self.assertEqual(
            text,
            """\
deb https://mirrors.aliyun.com/ubuntu/ jammy main restricted
deb https://mirrors.aliyun.com/ubuntu/ jammy-security main restricted
deb-src https://mirrors.aliyun.com/ubuntu/ jammy universe
# deb-src http://archive.ubuntu.com/ubuntu/ jammy main restricted
deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /
deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /
deb https://mirrors.aliyun.com/ubuntu/ jammy multiverse
""",
        )

    def test_unchanged_when_already_winner(self):
        mirrors = (Mirror("official", "http://archive.ubuntu.com/ubuntu"),)
        original = "deb http://archive.ubuntu.com/ubuntu/ jammy main restricted\n"
        text, changed = rewrite_sources(original, mirrors, mirrors[0])
        self.assertFalse(changed)
        self.assertEqual(text, original)


class ProbeAllTest(unittest.TestCase):
    def test_curl_command_matches_spec(self):
        config = load_config(SAMPLE)
        command = build_curl_command(config.mirrors[0], config, "/tmp/inrelease")
        self.assertEqual(
            command,
            [
                "curl",
                "--silent",
                "--show-error",
                "--max-redirs",
                "0",
                "--connect-timeout",
                "1",
                "--max-time",
                "2",
                "--output",
                "/tmp/inrelease",
                "--write-out",
                "%{http_code} %{time_total}",
                "https://mirrors.aliyun.com/ubuntu/dists/jammy/InRelease",
            ],
        )

    def test_parse_write_out(self):
        self.assertEqual(parse_write_out("200 0.116475\n"), (200, 0.116475))
        self.assertEqual(parse_write_out(""), (0, float("inf")))

    def test_deadline_drops_unfinished_probe(self):
        release = threading.Event()
        timed_out = threading.Event()
        config = Config(
            suite="jammy",
            probe=ProbeConfig(1, 2, 1),
            mirrors=(
                Mirror("aliyun", "https://mirrors.aliyun.com/ubuntu"),
                Mirror("official", "http://archive.ubuntu.com/ubuntu"),
            ),
        )

        def runner(mirror):
            if mirror.name == "official":
                release.wait(2)
                return _result(mirror.name, mirror.baseurl, 9.0)
            return _result(mirror.name, mirror.baseurl, 0.1)

        results = probe_all(config, runner, clock=lambda: 0.0, on_timeout=timed_out.set)
        release.set()
        self.assertTrue(timed_out.is_set())
        self.assertEqual([result.mirror.name for result in results], ["aliyun"])

    def test_timeout_callback_error_keeps_finished_probe(self):
        release = threading.Event()
        config = Config(
            suite="jammy",
            probe=ProbeConfig(1, 2, 1),
            mirrors=(
                Mirror("aliyun", "https://mirrors.aliyun.com/ubuntu"),
                Mirror("official", "http://archive.ubuntu.com/ubuntu"),
            ),
        )

        def runner(mirror):
            if mirror.name == "official":
                release.wait(2)
                return _result(mirror.name, mirror.baseurl, 9.0)
            return _result(mirror.name, mirror.baseurl, 0.1)

        def on_timeout():
            raise ProcessLookupError(1, "No such process")

        results = probe_all(config, runner, clock=lambda: 0.0, on_timeout=on_timeout)
        release.set()
        self.assertEqual([result.mirror.name for result in results], ["aliyun"])

    def test_curl_missing_unlinks_temp_and_prints_once(self):
        config = load_config(SAMPLE)
        session = CurlSession()
        runner = make_curl_runner(config, session)
        created_paths: list[str] = []
        real_named_temp = tempfile.NamedTemporaryFile

        def tracking_named_temp(*args, **kwargs):
            handle = real_named_temp(*args, **kwargs)
            created_paths.append(handle.name)
            return handle

        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch(
            "select_mirror.tempfile.NamedTemporaryFile", tracking_named_temp
        ), mock.patch(
            "select_mirror.subprocess.Popen",
            side_effect=FileNotFoundError(2, "No such file or directory: 'curl'"),
        ):
            results = probe_all(config, runner, clock=lambda: 0.0)
        for path in created_paths:
            self.assertFalse(os.path.exists(path), path)
        err = stderr.getvalue()
        self.assertEqual(err.count("error: curl:"), 1)
        self.assertEqual(len(results), len(config.mirrors))
        for result in results:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.body, "")

    def test_kill_all_continues_after_process_lookup_error(self):
        class FakeProc:
            def __init__(self, kill_error=None):
                self._kill_error = kill_error
                self.kill_called = False

            def poll(self):
                return None

            def kill(self):
                self.kill_called = True
                if self._kill_error is not None:
                    raise self._kill_error

        first = FakeProc(ProcessLookupError(1, "No such process"))
        second = FakeProc()
        session = CurlSession()
        session.add(first)
        session.add(second)
        session.kill_all()
        self.assertTrue(first.kill_called)
        self.assertTrue(second.kill_called)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path("test-tmp-sources")
        self.directory.mkdir(exist_ok=True)
        self.sources = self.directory / "sources.list"
        self.sources.write_text(SOURCES, encoding="utf-8")
        self.config_path = self.directory / "mirrors.yaml"
        self.config_path.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def _args(self):
        return ["--config", str(self.config_path), "--sources", str(self.sources)]

    def test_dry_run_prints_and_does_not_write(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                self._args() + ["--dry-run"],
                runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2 if mirror.name == "aliyun" else 0.9),
            )
        self.assertEqual(code, 0)
        self.assertIn("https://mirrors.aliyun.com/ubuntu/", stdout.getvalue())
        self.assertIn("selected name=aliyun", stderr.getvalue())
        self.assertIn("time_total=0.200000", stderr.getvalue())
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)
        self.assertEqual(list(self.directory.glob("sources.list.bak.*")), [])

    def test_apply_writes_backup(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                self._args(),
                runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2 if mirror.name == "aliyun" else 0.9),
                now=lambda: datetime(2026, 10, 8, 14, 23, 0),
            )
        self.assertEqual(code, 0)
        backup = self.directory / "sources.list.bak.20261008142300"
        self.assertEqual(backup.read_text(encoding="utf-8"), SOURCES)
        self.assertIn("deb https://mirrors.aliyun.com/ubuntu/ jammy main restricted\n", self.sources.read_text(encoding="utf-8"))
        self.assertIn("developer.download.nvidia.com", self.sources.read_text(encoding="utf-8"))

    def test_already_winner_does_not_write(self):
        self.sources.write_text("deb https://mirrors.aliyun.com/ubuntu/ jammy main restricted\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            code = main(
                self._args(),
                runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2 if mirror.name == "aliyun" else 0.9),
            )
        self.assertEqual(code, 0)
        self.assertEqual(list(self.directory.glob("sources.list.bak.*")), [])

    def test_no_valid_probe_leaves_file(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(self._args(), runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2, returncode=28))
        self.assertEqual(code, 1)
        self.assertIn("selected none", stderr.getvalue())
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)

    def test_bad_yaml_exits_2(self):
        self.config_path.write_text("suite:\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(self._args(), runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.1))
        self.assertEqual(code, 2)
        self.assertTrue(stderr.getvalue().startswith("error:"))

    def test_missing_sources_exits_4(self):
        self.sources.unlink()
        code = main(self._args(), runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.1))
        self.assertEqual(code, 4)
        self.assertFalse(self.sources.exists())

    def test_deadline_miss_does_not_write(self):
        state = {"now": 0.0}

        def clock():
            return state["now"]

        def runner(mirror):
            state["now"] = 100.0
            return _result(mirror.name, mirror.baseurl, 0.1)

        with redirect_stderr(io.StringIO()):
            code = main(self._args(), runner=runner, clock=clock)
        self.assertEqual(code, 3)
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)

    def test_unwritable_prints_like_dry_run(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()), mock.patch("select_mirror.os.access", return_value=False):
            code = main(
                self._args(),
                runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2 if mirror.name == "aliyun" else 0.9),
            )
        self.assertEqual(code, 0)
        self.assertIn("https://mirrors.aliyun.com/ubuntu/", stdout.getvalue())
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)

    def test_replace_failure_exits_4_and_keeps_original(self):
        with redirect_stderr(io.StringIO()), mock.patch("select_mirror.os.replace", side_effect=OSError("disk")):
            code = main(
                self._args(),
                runner=lambda mirror: _result(mirror.name, mirror.baseurl, 0.2 if mirror.name == "aliyun" else 0.9),
                now=lambda: datetime(2026, 10, 8, 14, 23, 1),
            )
        self.assertEqual(code, 4)
        self.assertEqual(self.sources.read_text(encoding="utf-8"), SOURCES)


if __name__ == "__main__":
    unittest.main()
