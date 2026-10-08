import unittest

from select_mirror import (
    ConfigError,
    Mirror,
    ProbeResult,
    load_config,
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
        text = open("mirrors.yaml", encoding="utf-8").read()
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


if __name__ == "__main__":
    unittest.main()
