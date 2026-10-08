#!/usr/bin/env python3
"""Select the fastest Ubuntu archive mirror and rewrite sources.list."""

from __future__ import annotations

from dataclasses import dataclass


class ConfigError(ValueError):
    """YAML text is missing or outside the supported subset."""


@dataclass(frozen=True)
class ProbeConfig:
    connect_timeout_sec: int
    max_time_sec: int
    deadline_sec: int


@dataclass(frozen=True)
class Mirror:
    name: str
    baseurl: str


@dataclass(frozen=True)
class Config:
    suite: str
    probe: ProbeConfig
    mirrors: tuple[Mirror, ...]


@dataclass(frozen=True)
class ProbeResult:
    mirror: Mirror
    http_code: int
    time_total: float
    body: str
    returncode: int


def is_valid(result: ProbeResult, suite: str) -> bool:
    if result.returncode != 0 or result.http_code != 200:
        return False
    needle = f"Codename: {suite}"
    return any(line.strip() == needle for line in result.body.splitlines())


def select_winner(results: list[ProbeResult], config: Config) -> ProbeResult | None:
    order = {mirror.name: index for index, mirror in enumerate(config.mirrors)}
    valid = [result for result in results if is_valid(result, config.suite)]
    if not valid:
        return None
    valid.sort(key=lambda result: (result.time_total, order.get(result.mirror.name, 10**9)))
    return valid[0]


def probe_url(mirror: Mirror, suite: str) -> str:
    return f"{mirror.baseurl}/dists/{suite}/InRelease"


def load_config(text: str) -> Config:
    suite = None
    timeouts: dict[str, int] = {}
    mirrors: list[Mirror] = []
    seen: set[str] = set()
    mode = None
    current: dict[str, str] | None = None

    def fail(lineno: int, message: str) -> None:
        raise ConfigError(f"line {lineno}: {message}")

    def finish_mirror(lineno: int) -> None:
        nonlocal current
        if current is None:
            return
        if "name" not in current or "baseurl" not in current:
            fail(lineno, "mirror needs name and baseurl")
        name = current["name"]
        baseurl = current["baseurl"]
        if name in seen:
            fail(lineno, f"duplicate name {name}")
        if not (baseurl.startswith("http://") or baseurl.startswith("https://")):
            fail(lineno, "baseurl must start with http:// or https://")
        if any(char.isspace() for char in baseurl):
            fail(lineno, "baseurl contains whitespace")
        seen.add(name)
        mirrors.append(Mirror(name=name, baseurl=baseurl.rstrip("/")))
        current = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            fail(lineno, "tabs are not allowed")
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if indent == 0:
            finish_mirror(lineno)
            key, value = _split_field(content, lineno, fail)
            if key == "suite":
                if not value:
                    fail(lineno, "suite is empty")
                suite = value
                mode = None
            elif key == "probe" and value == "":
                mode = "probe"
            elif key == "mirrors" and value == "":
                mode = "mirrors"
            else:
                fail(lineno, f"unexpected key {key}")
        elif mode == "probe" and indent == 2:
            key, value = _split_field(content, lineno, fail)
            if key not in ("connect_timeout_sec", "max_time_sec", "deadline_sec"):
                fail(lineno, f"unexpected probe key {key}")
            timeouts[key] = _positive_int(value, lineno, fail)
        elif mode == "mirrors" and indent == 2 and content.startswith("- "):
            finish_mirror(lineno)
            current = {}
            rest = content[2:].strip()
            if rest:
                key, value = _split_field(rest, lineno, fail)
                current[key] = value
        elif mode == "mirrors" and indent == 4 and current is not None:
            key, value = _split_field(content, lineno, fail)
            if key not in ("name", "baseurl"):
                fail(lineno, f"unexpected mirror key {key}")
            current[key] = value
        else:
            fail(lineno, "unsupported structure")
    finish_mirror(len(text.splitlines()) + 1)

    if not suite:
        raise ConfigError("suite is missing")
    if set(timeouts) != {"connect_timeout_sec", "max_time_sec", "deadline_sec"}:
        raise ConfigError("probe timeouts are incomplete")
    probe = ProbeConfig(
        connect_timeout_sec=timeouts["connect_timeout_sec"],
        max_time_sec=timeouts["max_time_sec"],
        deadline_sec=timeouts["deadline_sec"],
    )
    if not (probe.connect_timeout_sec <= probe.max_time_sec <= probe.deadline_sec):
        raise ConfigError("timeouts must satisfy connect <= max_time <= deadline")
    if not mirrors:
        raise ConfigError("mirrors is empty")
    return Config(suite=suite, probe=probe, mirrors=tuple(mirrors))


def _split_field(content: str, lineno: int, fail) -> tuple[str, str]:
    if ":" not in content:
        fail(lineno, "missing colon")
    key, value = content.split(":", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        fail(lineno, "empty key")
    return key, value


def _positive_int(value: str, lineno: int, fail) -> int:
    if not value.isdigit() or value.startswith("0"):
        fail(lineno, f"expected a positive integer, got {value}")
    return int(value)
