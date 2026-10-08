import unittest

from select_mirror import ConfigError, load_config, probe_url


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


if __name__ == "__main__":
    unittest.main()
