"""Pick the fastest package mirror for the distro described in the YAML list."""

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
class Profile:
    id: str
    match: tuple[str, ...]
    suite_keys: tuple[str, ...]
    fixed_suite: str
    probe_path: str
    marker: str
    bases: tuple[str, ...]
    sources_parts: tuple[str, ...]
    also: tuple[str, ...]
    mirrors: tuple[Mirror, ...]


@dataclass(frozen=True)
class Catalog:
    probe: ProbeConfig
    release_parts: tuple[str, ...]
    profiles: tuple[Profile, ...]


@dataclass(frozen=True)
class Config:
    probe: ProbeConfig
    mirrors: tuple[Mirror, ...]
    suite: str = ""
    probe_path: str = "dists/{suite}/InRelease"
    marker: str = "Codename: {suite}"
    bases: tuple[str, ...] = ()
    sources_parts: tuple[str, ...] = ()
    also: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeResult:
    mirror: Mirror
    http_code: int
    time_total: float
    body: str
    returncode: int
    final_url: str = ""


def rooted(*parts: str) -> Path:
    return Path(os.sep).joinpath(*parts)


def parse_os_release(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        info[key.strip()] = value
    return info


def detect(catalog: Catalog, release_text: str) -> tuple[str, str]:
    info = parse_os_release(release_text)
    distro_id = info.get("ID", "")
    like = info.get("ID_LIKE", "").split()
    for candidate in (distro_id, *like):
        if not candidate:
            continue
        for profile in catalog.profiles:
            if candidate not in profile.match:
                continue
            suite = profile.fixed_suite
            if not suite:
                for key in profile.suite_keys:
                    if info.get(key):
                        suite = info[key]
                        break
            if not suite:
                raise ConfigError("suite is missing")
            return profile.id, suite
    raise ConfigError("no matching distro")


def activate(catalog: Catalog, distro: str, suite: str) -> Config:
    if not suite:
        raise ConfigError("suite is missing")
    for profile in catalog.profiles:
        if profile.id == distro or distro in profile.match:
            return Config(
                probe=catalog.probe,
                mirrors=profile.mirrors,
                suite=suite,
                probe_path=profile.probe_path,
                marker=profile.marker,
                bases=profile.bases,
                sources_parts=profile.sources_parts,
                also=profile.also,
            )
    raise ConfigError(f"unknown distro {distro}")


def is_valid(result: ProbeResult, config: Config) -> bool:
    if result.returncode != 0 or result.http_code != 200:
        return False
    if result.final_url:
        expected = urlparse(probe_url(result.mirror, config.suite, config.probe_path)).hostname
        if urlparse(result.final_url).hostname != expected:
            return False
    marker = config.marker.format(suite=config.suite)
    if config.marker == "digits":
        return result.body.strip().isdigit()
    if config.marker == "any":
        return bool(result.body.strip())
    if config.marker.startswith("Codename:"):
        return any(line.strip() == marker for line in result.body.splitlines())
    return marker in result.body


def select_winner(results: list[ProbeResult], config: Config) -> ProbeResult | None:
    order = {mirror.name: index for index, mirror in enumerate(config.mirrors)}
    valid = [result for result in results if is_valid(result, config)]
    if not valid:
        return None
    valid.sort(key=lambda result: (result.time_total, order.get(result.mirror.name, 10**9)))
    return valid[0]


def probe_url(mirror: Mirror, suite: str, probe_path: str = "dists/{suite}/InRelease") -> str:
    relative = probe_path.format(suite=suite).lstrip("/")
    return f"{mirror.baseurl}/{relative}"


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
        probe_url(mirror, config.suite, config.probe_path),
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
        self._curl_start_error_reported = False

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)

    def report_curl_start_failure(self, exc: BaseException) -> None:
        with self._lock:
            if self._curl_start_error_reported:
                return
            print(f"error: curl: {exc}", file=sys.stderr)
            self._curl_start_error_reported = True

    def kill_all(self) -> None:
        with self._lock:
            for proc in self._procs:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        continue


def _invalid_probe_result(mirror: Mirror) -> ProbeResult:
    return ProbeResult(
        mirror=mirror,
        http_code=0,
        time_total=float("inf"),
        body="",
        returncode=1,
    )


def make_curl_runner(config: Config, session: CurlSession) -> Callable[[Mirror], ProbeResult]:
    def runner(mirror: Mirror) -> ProbeResult:
        handle = tempfile.NamedTemporaryFile(prefix=f"fs-{mirror.name}-", delete=False)
        output_path = handle.name
        handle.close()
        try:
            command = build_curl_command(mirror, config, output_path)
            try:
                proc = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
            except FileNotFoundError as exc:
                session.report_curl_start_failure(exc)
                return _invalid_probe_result(mirror)
            session.add(proc)
            try:
                stdout, _stderr = proc.communicate()
            except Exception:
                return _invalid_probe_result(mirror)
            try:
                body = open(output_path, encoding="utf-8", errors="replace").read()
            except OSError:
                body = ""
            http_code, time_total = parse_write_out(stdout or "")
            return ProbeResult(
                mirror=mirror,
                http_code=http_code,
                time_total=time_total,
                body=body,
                returncode=proc.returncode if proc.returncode is not None else 1,
            )
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass

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
        shutdown_kwargs = {"wait": False}
        if sys.version_info >= (3, 9):
            shutdown_kwargs["cancel_futures"] = True
        pool.shutdown(**shutdown_kwargs)


def _known_bases(mirrors: tuple[Mirror, ...], bases: tuple[str, ...]) -> list[str]:
    found = {mirror.baseurl.rstrip("/") for mirror in mirrors}
    found.update(base.rstrip("/") for base in bases)
    return sorted(found, key=len, reverse=True)


def rewrite_sources(
    text: str,
    mirrors: tuple[Mirror, ...],
    winner: Mirror,
    bases: tuple[str, ...] = (),
) -> tuple[str, bool]:
    winner_uri = winner.baseurl.rstrip("/")
    known = _known_bases(mirrors, bases)
    changed = False
    lines = []
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        stripped = body.lstrip()
        if stripped.startswith("#") or re.match(r"deb(?:-src)?\s+\[", stripped):
            lines.append(line)
            continue
        updated = body
        for base in known:
            idx = updated.find(base)
            if idx < 0:
                continue
            if idx > 0 and updated[idx - 1] not in " \t='\"":
                continue
            end = idx + len(base)
            if end < len(updated) and updated[end] not in "/ \t":
                continue
            updated = updated[:idx] + winner_uri + updated[end:]
            break
        if updated != body:
            changed = True
        lines.append(updated + newline)
    return "".join(lines), changed


def load_config(text: str) -> Catalog:
    timeouts: dict[str, int] = {}
    release: list[str] = []
    profiles: list[Profile] = []
    seen: set[str] = set()
    mode = None
    profile: dict | None = None
    mirror: dict[str, str] | None = None

    def fail(lineno: int, message: str) -> None:
        raise ConfigError(f"line {lineno}: {message}")

    def finish_mirror(lineno: int) -> None:
        nonlocal mirror
        if mirror is None:
            return
        if profile is None:
            fail(lineno, "mirror outside distro")
        if "name" not in mirror or "baseurl" not in mirror:
            fail(lineno, "mirror needs name and baseurl")
        name = mirror["name"]
        baseurl = mirror["baseurl"]
        if name in seen:
            fail(lineno, f"duplicate name {name}")
        if not (baseurl.startswith("http://") or baseurl.startswith("https://")):
            fail(lineno, "baseurl must start with http:// or https://")
        if any(char.isspace() for char in baseurl):
            fail(lineno, "baseurl contains whitespace")
        seen.add(name)
        profile["mirrors"].append(Mirror(name=name, baseurl=baseurl.rstrip("/")))
        mirror = None

    def finish_profile(lineno: int) -> None:
        nonlocal profile
        finish_mirror(lineno)
        if profile is None:
            return
        if "id" not in profile:
            fail(lineno, "distro needs id")
        match = tuple(profile.get("match", profile["id"]).split())
        suite_keys = tuple(profile.get("suite_keys", "").split())
        fixed_suite = profile.get("suite", "")
        if not suite_keys and not fixed_suite:
            fail(lineno, "distro needs suite_keys or suite")
        profiles.append(
            Profile(
                id=profile["id"],
                match=match,
                suite_keys=suite_keys,
                fixed_suite=fixed_suite,
                probe_path=profile.get("probe_path", "dists/{suite}/InRelease"),
                marker=profile.get("marker", "Codename: {suite}"),
                bases=tuple(profile.get("bases", "").split()),
                sources_parts=tuple(profile.get("sources", "").split()),
                also=tuple(profile.get("also", "").split()),
                mirrors=tuple(profile["mirrors"]),
            )
        )
        profile = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            fail(lineno, "tabs are not allowed")
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if indent == 0:
            finish_profile(lineno)
            key, value = _split_field(content, lineno, fail)
            if key == "probe" and value == "":
                mode = "probe"
            elif key == "release" and value == "":
                mode = "release"
            elif key == "distros" and value == "":
                mode = "distros"
            else:
                fail(lineno, f"unexpected key {key}")
        elif mode == "probe" and indent == 2:
            key, value = _split_field(content, lineno, fail)
            if key not in ("connect_timeout_sec", "max_time_sec", "deadline_sec"):
                fail(lineno, f"unexpected probe key {key}")
            timeouts[key] = _positive_int(value, lineno, fail)
        elif mode == "release" and indent == 2 and content.startswith("- "):
            release.append(content[2:].strip())
        elif mode == "distros" and indent == 2 and content.startswith("- "):
            finish_profile(lineno)
            profile = {"mirrors": []}
            rest = content[2:].strip()
            if rest:
                key, value = _split_field(rest, lineno, fail)
                profile[key] = value
        elif mode == "distros" and indent == 4 and profile is not None and content == "mirrors:":
            finish_mirror(lineno)
        elif mode == "distros" and indent == 4 and profile is not None:
            finish_mirror(lineno)
            key, value = _split_field(content, lineno, fail)
            profile[key] = value
        elif (
            mode == "distros"
            and indent == 6
            and profile is not None
            and content.startswith("- ")
        ):
            finish_mirror(lineno)
            mirror = {}
            rest = content[2:].strip()
            if rest:
                key, value = _split_field(rest, lineno, fail)
                mirror[key] = value
        elif mode == "distros" and indent == 8 and mirror is not None:
            key, value = _split_field(content, lineno, fail)
            if key not in ("name", "baseurl"):
                fail(lineno, f"unexpected mirror key {key}")
            mirror[key] = value
        else:
            fail(lineno, "unsupported structure")
    finish_profile(len(text.splitlines()) + 1)

    if set(timeouts) != {"connect_timeout_sec", "max_time_sec", "deadline_sec"}:
        raise ConfigError("probe timeouts are incomplete")
    probe = ProbeConfig(
        connect_timeout_sec=timeouts["connect_timeout_sec"],
        max_time_sec=timeouts["max_time_sec"],
        deadline_sec=timeouts["deadline_sec"],
    )
    if not (probe.connect_timeout_sec <= probe.max_time_sec <= probe.deadline_sec):
        raise ConfigError("timeouts must satisfy connect <= max_time <= deadline")
    if not profiles:
        raise ConfigError("distros is empty")
    if not release:
        raise ConfigError("release is missing")
    return Catalog(probe=probe, release_parts=tuple(release), profiles=tuple(profiles))


def _split_field(content: str, lineno: int, fail) -> tuple[str, str]:
    if ":" not in content:
        fail(lineno, "missing colon")
    key, value = content.split(":", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
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


def collect_source_files(path: Path, extra_dirs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    if path.is_dir():
        files.extend(sorted(item for item in path.iterdir() if item.is_file() and not item.name.startswith(".")))
    elif path.is_file():
        files.append(path)
    for name in extra_dirs:
        directory = path.parent / name
        if not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            if item.is_file() and item.suffix in (".list", ".sources"):
                files.append(item)
    return files


def _resolve(catalog: Catalog, distro: str, suite: str) -> Config:
    return activate(catalog, distro, suite)


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
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--distro")
    parser.add_argument("--suite")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    start = clock()
    try:
        catalog = load_config(args.config.read_text(encoding="utf-8"))
        if args.distro or args.suite:
            if not args.distro or not args.suite:
                raise ConfigError("distro and suite are set together")
            config = _resolve(catalog, args.distro, args.suite)
        else:
            release_path = args.release if args.release is not None else rooted(*catalog.release_parts)
            config = _resolve(catalog, *detect(catalog, release_path.read_text(encoding="utf-8")))
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sources = args.sources if args.sources is not None else rooted(*config.sources_parts)
    files = collect_source_files(sources, config.also)
    if not files:
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
    planned: list[tuple[Path, str]] = []
    for path in files:
        new_text, changed = rewrite_sources(
            path.read_text(encoding="utf-8"),
            config.mirrors,
            winner.mirror,
            config.bases,
        )
        if changed:
            planned.append((path, new_text))
    if not planned:
        return 0
    if args.dry_run or any(not os.access(path, os.W_OK) for path, _text in planned):
        for _path, new_text in planned:
            sys.stdout.write(new_text)
        return 0
    try:
        stamp = now().strftime("%Y%m%d%H%M%S")
        for path, new_text in planned:
            apply_sources(path, new_text, stamp)
    except OSError:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
