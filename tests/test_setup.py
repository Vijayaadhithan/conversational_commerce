"""Exercise installer behavior with all package/service commands simulated."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FAKE_TOOL = r"""
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_SETUP_DIR"])
log = root / "commands.jsonl"
with log.open("a") as stream:
    stream.write(json.dumps([name] + args) + "\n")
prefix = root / "brew"
major = os.environ.get("TEST_SETUP_PG_MAJOR", "16")
pg = prefix / ("opt/postgresql@" + major)
bindir = pg / "bin"
sharedir = pg / "share"
libdir = pg / "lib"

def copy_tool(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sys.argv[0], target)
    target.chmod(0o755)

if name in ("fake-python", "python"):
    if args[:2] == ["-m", "venv"]:
        target = pathlib.Path(args[2]) / "bin/python"
        target.parent.mkdir(parents=True)
        shutil.copyfile(sys.argv[0], target)
        target.chmod(0o755)
    if args[:3] == ["-m", "pip", "install"] and os.environ.get("TEST_SETUP_FAIL_PIP"):
        sys.exit(42)
elif name == "uname":
    print(os.environ.get("TEST_SETUP_OS", "Darwin"))
elif name == "id":
    print("0")
elif name == "brew":
    if args == ["--prefix"]:
        print(prefix)
    if args[:2] == ["services", "start"]:
        (root / ("redis.ready" if args[2] == "redis" else "pg.ready")).touch()
    if args == ["install", "postgresql@18"]:
        for directory in (sharedir / "extension", libdir, pg / "include/server"):
            directory.mkdir(parents=True, exist_ok=True)
        (pg / "include/server/postgres.h").touch()
        for executable in ("pg_config", "pg_ctl", "pg_isready"):
            copy_tool(bindir / executable)
    if args == ["install", "redis"]:
        for executable in ("redis-cli", "redis-server"):
            copy_tool(root / "bin" / executable)
elif name == "pg_config":
    outputs = {"--version": "PostgreSQL " + major + ".14", "--bindir": str(bindir),
               "--sharedir": str(sharedir), "--pkglibdir": str(libdir),
               "--includedir-server": str(pg / "include/server")}
    print(outputs[args[0]])
elif name == "pg_isready":
    sys.exit(0 if (root / "pg.ready").exists() else 1)
elif name == "redis-cli":
    if (root / "redis.ready").exists():
        print("NOAUTH Authentication required." if os.environ.get("TEST_SETUP_REDIS_AUTH") else "PONG")
    else:
        sys.exit(1)
elif name == "git":
    if args[0] != "clone" or args[-2] != "https://github.com/pgvector/pgvector.git":
        sys.exit("Unexpected git command")
    pathlib.Path(args[-1]).mkdir(parents=True)
elif name == "make":
    if args[-1] == "install":
        (sharedir / "extension/vector.control").touch()
        (libdir / "vector.so").touch()
elif name == "xcrun":
    print("clang")
elif name in ("service", "systemctl"):
    (root / ("redis.ready" if "redis-server" in args else "pg.ready")).touch()
elif name not in ("cc", "apt-get"):
    sys.exit("Unexpected command: " + name)
"""


@pytest.fixture
def installer(tmp_path):
    checkout = tmp_path / "checkout with spaces"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/setup.sh", checkout / "scripts/setup.sh")
    for filename in (
        ".env.example",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-reranker.txt",
    ):
        shutil.copyfile(ROOT / filename, checkout / filename)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Keep host PostgreSQL/Redis executables out of PATH, even when simulating
    # their absence on a developer machine that already has both installed.
    for name in ("bash", "dirname", "cat", "env", "sed", "mktemp", "rm", "sleep"):
        (binaries / name).symlink_to(shutil.which(name))
    pg = tmp_path / "brew/opt/postgresql@16"
    for directory in (pg / "bin", pg / "share/extension", pg / "lib", pg / "include/server"):
        directory.mkdir(parents=True)
    (pg / "include/server/postgres.h").touch()

    def tool(target):
        target.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
        target.chmod(0o755)

    for name in (
        "fake-python",
        "uname",
        "id",
        "brew",
        "redis-cli",
        "redis-server",
        "git",
        "make",
        "cc",
        "xcrun",
        "apt-get",
        "service",
        "systemctl",
    ):
        tool(binaries / name)
    for name in ("pg_config", "pg_ctl", "pg_isready"):
        tool(pg / "bin" / name)
    env = dict(
        os.environ,
        TEST_SETUP_DIR=str(tmp_path),
        TEST_SETUP_OS="Darwin",
        PYTHON_BIN=str(binaries / "fake-python"),
        PG_CONFIG=str(pg / "bin/pg_config"),
        PATH=str(binaries),
    )

    def run(*args, changes=None):
        result = subprocess.run(
            ["bash", str(checkout / "scripts/setup.sh"), *args],
            cwd=tmp_path,
            env={**env, **(changes or {})},
            capture_output=True,
            text=True,
        )
        events = [
            json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()
        ]
        return result, events

    return checkout, tmp_path, pg, run


def test_creates_private_env_and_reuses_environment_without_model_downloads(installer):
    checkout, _, _, run = installer
    result, events = run("--with-reranker", "--dev")
    assert result.returncode == 0, result.stderr
    env_file = checkout / ".env"
    assert env_file.read_bytes() == (checkout / ".env.example").read_bytes()
    assert env_file.stat().st_mode & 0o777 == 0o600
    env_file.write_text("CUSTOM_SETTING=preserve-me\n")
    result, events = run()
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "CUSTOM_SETTING=preserve-me\n"
    assert sum(event[1:3] == ["-m", "venv"] for event in events) == 1
    installs = [event for event in events if event[1:4] == ["-m", "pip", "install"]]
    assert any(str(checkout / "requirements-reranker.txt") in event for event in installs)
    assert any(str(checkout / "requirements-dev.txt") in event for event in installs)
    assert not any(event[0] in ("brew", "apt-get", "git") for event in events)
    assert not (checkout / "models").exists() and not (checkout / "data").exists()


def test_dry_run_with_all_flags_has_no_mutations(installer):
    checkout, _, _, run = installer
    result, events = run("--dry-run", "--with-services", "--with-reranker", "--dev")
    assert result.returncode == 0, result.stderr
    assert "pgvector v0.8.7" in result.stdout
    assert not (checkout / ".venv").exists() and not (checkout / ".env").exists()
    assert not any(event[0] in ("git", "make", "apt-get") for event in events)
    assert not any(event[0] == "brew" and event[1:] != ["--prefix"] for event in events)
    assert not any(event[1:3] == ["-m", "pip"] for event in events)


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
def test_services_build_for_existing_postgres_and_reuse_on_second_run(installer, platform):
    _, temp, pg, run = installer
    result, first_events = run("--with-services", changes={"TEST_SETUP_OS": platform})
    assert result.returncode == 0, result.stderr
    assert (pg / "share/extension/vector.control").exists()
    assert (pg / "lib/vector.so").exists()
    assert (temp / "pg.ready").exists() and (temp / "redis.ready").exists()
    assert any(event[0] == "git" and "v0.8.7" in event for event in first_events)
    assert not any(event[0] in ("brew", "apt-get") and "install" in event for event in first_events)
    result, all_events = run("--with-services", changes={"TEST_SETUP_OS": platform})
    assert result.returncode == 0, result.stderr
    new_events = all_events[len(first_events) :]
    assert not any(event[0] in ("git", "make", "service", "systemctl") for event in new_events)
    assert not any(event[0] == "brew" and "start" in event for event in new_events)


def test_preserves_authenticated_redis_without_restarting(installer):
    _, temp, _, run = installer
    (temp / "pg.ready").touch()
    (temp / "redis.ready").touch()
    result, events = run("--with-services", changes={"TEST_SETUP_REDIS_AUTH": "1"})
    assert result.returncode == 0, result.stderr
    assert "requires authentication" in result.stdout
    assert not any(event[0] == "brew" and "start" in event for event in events)


def test_fresh_homebrew_service_installation(installer):
    _, temp, pg, run = installer
    shutil.rmtree(pg)
    for name in ("redis-cli", "redis-server"):
        (temp / "bin" / name).unlink()
    result, events = run("--with-services", changes={"PG_CONFIG": "", "TEST_SETUP_PG_MAJOR": "18"})
    assert result.returncode == 0, result.stderr
    assert ["brew", "install", "postgresql@18"] in events
    assert ["brew", "install", "redis"] in events
    assert (temp / "brew/opt/postgresql@18/lib/vector.so").exists()


def test_linux_installs_matching_server_headers(installer):
    _, _, pg, run = installer
    (pg / "include/server/postgres.h").unlink()
    result, events = run("--with-services", changes={"TEST_SETUP_OS": "Linux"})
    assert result.returncode == 0, result.stderr
    assert ["apt-get", "update"] in events
    assert any(event[0] == "apt-get" and "postgresql-server-dev-16" in event for event in events)
    assert not any(event[0] == "apt-get" and "postgresql" in event for event in events)


def test_install_failure_stops_before_service_or_env_changes(installer):
    checkout, _, _, run = installer
    result, events = run("--with-services", changes={"TEST_SETUP_FAIL_PIP": "1"})
    assert result.returncode == 42
    assert not (checkout / ".env").exists()
    assert not any(event[0] in ("git", "make", "service", "systemctl") for event in events)


def test_broken_existing_venv_is_not_overwritten(installer):
    checkout, _, _, run = installer
    (checkout / ".venv").mkdir()
    result, events = run()
    assert result.returncode != 0
    assert "will not be overwritten" in result.stderr
    assert not any(event[1:3] == ["-m", "venv"] for event in events)


def test_help_and_unknown_flags_have_no_changes(tmp_path):
    for args, expected in [(["--help"], 0), (["--download-models"], 1)]:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/setup.sh"), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected
        assert "Usage:" in result.stdout + result.stderr
