#!/usr/bin/env python3
"""Select the fastest Ubuntu archive mirror and rewrite sources.list."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


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


def build_curl_command(mirror: Mirror, config: Config, output_path: str) -> list[str]:
    return [
        "curl",
        "--silent",
        "--show-error",
        "--max-redirs",
        "0",
        "--connect-timeout",
        str(config.probe.connect_timeout_sec),
        "--max-time",
        str(config.probe.max_time_sec),
        "--output",
        output_path,
        "--write-out",
        "%{http_code} %{time_total}",
        probe_url(mirror, config.suite),
    ]


def parse_write_out(stdout: str) -> tuple[int, float]:
    parts = stdout.split()
    if len(parts) != 2:
        return 0, float("inf")
    try:
        return int(parts[0]), float(parts[1])
    except ValueError:
        return 0, float("inf")


class CurlSession:
    def __init__(self) -> None:
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)

    def kill_all(self) -> None:
        with self._lock:
            for proc in self._procs:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        continue


def make_curl_runner(config: Config, session: CurlSession) -> Callable[[Mirror], ProbeResult]:
    def runner(mirror: Mirror) -> ProbeResult:
        handle = tempfile.NamedTemporaryFile(prefix=f"fs-{mirror.name}-", delete=False)
        output_path = handle.name
        handle.close()
        command = build_curl_command(mirror, config, output_path)
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        session.add(proc)
        stdout, _stderr = proc.communicate()
        try:
            body = open(output_path, encoding="utf-8", errors="replace").read()
        except OSError:
            body = ""
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass
        http_code, time_total = parse_write_out(stdout or "")
        return ProbeResult(
            mirror=mirror,
            http_code=http_code,
            time_total=time_total,
            body=body,
            returncode=proc.returncode if proc.returncode is not None else 1,
        )

    return runner


def probe_all(
    config: Config,
    runner: Callable[[Mirror], ProbeResult],
    clock: Callable[[], float] = time.monotonic,
    on_timeout: Callable[[], None] = lambda: None,
) -> list[ProbeResult]:
    start = clock()
    limit = config.probe.deadline_sec - 0.2
    pool = ThreadPoolExecutor(max_workers=max(1, len(config.mirrors)))
    try:
        futures = [pool.submit(runner, mirror) for mirror in config.mirrors]
        remaining = limit - (clock() - start)
        done, not_done = wait(futures, timeout=max(0.0, remaining))
        if not_done:
            try:
                on_timeout()
            except OSError:
                pass
        results = []
        for future in done:
            try:
                results.append(future.result())
            except Exception:
                continue
        return results
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


_DEB_RE = re.compile(r"^(?P<type>deb(?:-src)?)(?P<sp>\s+)(?P<uri>\S+)(?P<rest>\s+.*)$")


def rewrite_sources(
    text: str, mirrors: tuple[Mirror, ...], winner: Mirror
) -> tuple[str, bool]:
    winner_uri = winner.baseurl.rstrip("/") + "/"
    hosts = {"archive.ubuntu.com", "security.ubuntu.com"}
    for mirror in mirrors:
        hostname = urlparse(mirror.baseurl).hostname
        if hostname:
            hosts.add(hostname)
    changed = False
    lines = []
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        stripped = body.lstrip()
        if stripped.startswith("#") or re.match(r"deb(?:-src)?\s+\[", stripped):
            lines.append(line)
            continue
        match = _DEB_RE.match(body)
        if not match:
            lines.append(line)
            continue
        uri = match.group("uri")
        if urlparse(uri).hostname not in hosts:
            lines.append(line)
            continue
        if uri != winner_uri:
            changed = True
        lines.append(
            f"{match.group('type')}{match.group('sp')}{winner_uri}{match.group('rest')}{newline}"
        )
    return "".join(lines), changed


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


def apply_sources(path: Path, new_text: str, stamp: str) -> None:
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    original = path.read_bytes()
    mode = path.stat().st_mode
    backup.write_bytes(original)
    tmp = path.with_name(f"{path.name}.tmp.{stamp}")
    try:
        tmp.write_bytes(new_text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def main(
    argv: list[str] | None = None,
    runner: Callable[[Mirror], ProbeResult] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
) -> int:
    if now is None:
        now = datetime.now
    parser = argparse.ArgumentParser(prog="select_mirror.py")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "mirrors.yaml")
    parser.add_argument("--sources", type=Path, default=Path("/etc/apt/sources.list"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    start = clock()
    try:
        config = load_config(args.config.read_text(encoding="utf-8"))
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.sources.is_file():
        print("error: sources file not found", file=sys.stderr)
        return 4
    if runner is None:
        session = CurlSession()
        runner = make_curl_runner(config, session)
        results = probe_all(config, runner, clock=clock, on_timeout=session.kill_all)
    else:
        results = probe_all(config, runner, clock=clock)
    winner = select_winner(results, config)
    if winner is None:
        print("selected none", file=sys.stderr)
        return 1
    print(
        f"selected name={winner.mirror.name} baseurl={winner.mirror.baseurl} time_total={winner.time_total:.6f}",
        file=sys.stderr,
    )
    if clock() - start > config.probe.deadline_sec:
        return 3
    new_text, changed = rewrite_sources(args.sources.read_text(encoding="utf-8"), config.mirrors, winner.mirror)
    if not changed:
        return 0
    if args.dry_run or not os.access(args.sources, os.W_OK):
        sys.stdout.write(new_text)
        return 0
    try:
        apply_sources(args.sources, new_text, now().strftime("%Y%m%d%H%M%S"))
    except OSError:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
