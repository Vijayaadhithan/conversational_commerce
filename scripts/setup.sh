#!/usr/bin/env bash
# Bootstrap dependencies only. Model weights and catalogs are managed separately.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
with_services=0
with_reranker=0
with_dev=0
dry_run=0
apt_updated=0
pg_config_bin=""
brew_prefix=""

usage() {
    cat <<'EOF'
Usage: bash scripts/setup.sh [options]

  --with-services  Install/reuse local PostgreSQL and Redis; start services
  --with-reranker  Install local reranker Python libraries (no model weights)
  --dev            Install testing dependencies
  --dry-run        Show planned changes without installing or writing files
  -h, --help       Show this help

Core setup supports macOS/Linux with Python 3.12+. System package setup supports
Homebrew on macOS and APT on Debian/Ubuntu. Install Homebrew separately if needed.
PYTHON_BIN can select a Python executable. PG_CONFIG can select an existing
PostgreSQL installation, for example /opt/homebrew/opt/postgresql@16/bin/pg_config.

Existing .env files and database contents are preserved. This script does not
install Ollama, download models/catalogs, create databases, or enable extensions
inside databases. Run as your normal user; system installation may invoke sudo.
EOF
}

log() { printf '\n%s\n' "$*"; }
die() { printf 'Setup error: %s\n' "$*" >&2; exit 1; }
run() {
    if (( dry_run )); then
        printf '[dry-run] '
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}
admin() {
    if [[ "$(id -u)" == 0 ]]; then
        run "$@"
    else
        command -v sudo >/dev/null || die "sudo is required for system package installation."
        run sudo -- "$@"
    fi
}
for option in "$@"; do
    case "$option" in
        --with-services) with_services=1 ;;
        --with-reranker) with_reranker=1 ;;
        --dev) with_dev=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "Unknown option: $option" ;;
    esac
done

platform="$(uname -s)"
case "$platform" in
    Darwin|Linux) ;;
    *) die "Unsupported OS: $platform. Use macOS or Linux (including WSL)." ;;
esac

require_package_manager() {
    if [[ "$platform" == Darwin ]]; then
        command -v brew >/dev/null || die "Install Homebrew first: https://brew.sh"
        brew_prefix="$(brew --prefix)"
    else
        command -v apt-get >/dev/null || die "System setup supports Debian/Ubuntu APT. Install prerequisites manually on this Linux distribution."
    fi
}

apt_install() {
    if (( ! apt_updated )); then
        admin apt-get update
        apt_updated=1
    fi
    admin apt-get install -y --no-install-recommends "$@"
}

valid_python() {
    [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1
}

select_python() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        python_bin="$(command -v "$PYTHON_BIN" || true)"
        [[ -n "$python_bin" ]] && valid_python "$python_bin" || die "PYTHON_BIN must select Python 3.12 or later."
        return
    fi
    if [[ -e "$project_root/.venv" ]]; then
        python_bin="$project_root/.venv/bin/python"
        valid_python "$python_bin" || die "Existing .venv is unusable or older than Python 3.12. Move it aside explicitly before rerunning."
        return
    fi
    local candidate resolved
    for candidate in python3.14 python3.13 python3.12 python3; do
        resolved="$(command -v "$candidate" || true)"
        if [[ -n "$resolved" ]] && valid_python "$resolved"; then
            python_bin="$resolved"
            return
        fi
    done
    require_package_manager
    if [[ "$platform" == Darwin ]]; then
        run brew install python@3.12
        python_bin="$brew_prefix/opt/python@3.12/bin/python3.12"
    else
        apt_install python3 python3-venv python3-pip
        python_bin="$(command -v python3 || true)"
    fi
    if (( ! dry_run )); then
        valid_python "$python_bin" || die "Your package repository does not provide Python 3.12+. Install it separately and set PYTHON_BIN."
    elif [[ -z "$python_bin" ]]; then
        python_bin=python3
    fi
}

if (( with_services )); then
    require_package_manager
fi
select_python
log "Project: $project_root"
log "Preparing Python environment"
if [[ -e "$project_root/.venv" ]]; then
    valid_python "$project_root/.venv/bin/python" || die "Existing .venv is unusable; it will not be overwritten."
    log "Reusing .venv"
else
    if (( ! dry_run )) && ! "$python_bin" -c 'import venv, ensurepip' >/dev/null 2>&1; then
        require_package_manager
        if [[ "$platform" == Linux ]]; then
            python_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            apt_install "python${python_version}-venv"
        else
            die "Selected Python lacks venv/ensurepip. Use a complete Python installation."
        fi
    fi
    run "$python_bin" -m venv "$project_root/.venv"
fi
venv_python="$project_root/.venv/bin/python"
requirements=(-r "$project_root/requirements.txt")
if (( with_reranker )); then
    requirements+=(-r "$project_root/requirements-reranker.txt")
fi
if (( with_dev )); then
    requirements+=(-r "$project_root/requirements-dev.txt")
fi
run "$venv_python" -m pip install "${requirements[@]}"

if [[ -e "$project_root/.env" || -L "$project_root/.env" ]]; then
    log "Preserving existing .env"
elif (( dry_run )); then
    log "[dry-run] Create .env from .env.example with permissions 600"
else
    # noclobber also protects against overwriting a file created concurrently.
    (umask 077; set -o noclobber; cat "$project_root/.env.example" > "$project_root/.env")
    log "Created .env from .env.example"
fi

discover_postgres() {
    if [[ -n "${PG_CONFIG:-}" ]]; then
        pg_config_bin="$(command -v "$PG_CONFIG" || true)"
        [[ -x "$pg_config_bin" ]] || die "PG_CONFIG must point to a working pg_config executable."
        return
    fi
    local found candidate
    found="$(command -v pg_config || true)"
    if [[ -n "$found" && -x "$("$found" --bindir)/pg_ctl" ]]; then
        pg_config_bin="$found"
        return
    fi
    local configs=()
    if [[ "$platform" == Darwin ]]; then
        for candidate in "$brew_prefix"/opt/postgresql*/bin/pg_config; do
            [[ ! -x "$candidate" ]] || configs+=("$candidate")
        done
    else
        for candidate in /usr/lib/postgresql/*/bin/pg_config; do
            [[ ! -x "$candidate" ]] || configs+=("$candidate")
        done
    fi
    if (( ${#configs[@]} > 1 )); then
        die "Multiple PostgreSQL installations found. Set PG_CONFIG to the intended installation."
    elif (( ${#configs[@]} == 1 )); then
        pg_config_bin="${configs[0]}"
    fi
}

configure_postgres() {
    if (( dry_run )) && [[ ! -x "$pg_config_bin" ]]; then
        return
    fi
    pg_major="$("$pg_config_bin" --version | sed -E 's/^PostgreSQL ([0-9]+).*/\1/')"
    pg_bindir="$("$pg_config_bin" --bindir)"
    [[ -x "$pg_bindir/pg_ctl" ]] || die "Selected PG_CONFIG has no PostgreSQL server installation."
}

start_and_check_services() {
    if (( dry_run )); then
        log "[dry-run] Start PostgreSQL and Redis only if unavailable, then check local readiness"
        return
    fi
    if ! "$pg_bindir/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
        if [[ "$platform" == Darwin ]]; then
            case "$pg_config_bin" in
                "$brew_prefix"/opt/postgresql*/bin/pg_config|"$brew_prefix"/Cellar/postgresql*/bin/pg_config) ;;
                *) die "Selected PostgreSQL is not Homebrew-managed. Start it manually and rerun." ;;
            esac
            run brew services start "postgresql@$pg_major"
        elif command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
            admin systemctl start postgresql
        elif command -v service >/dev/null; then
            admin service postgresql start
        else
            die "No service manager detected. Start PostgreSQL manually and rerun."
        fi
    fi
    local reply
    reply="$(redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null || true)"
    if [[ "$reply" != PONG && "$reply" != *NOAUTH* && "$reply" != *WRONGPASS* ]]; then
        if [[ "$platform" == Darwin ]]; then
            run brew services start redis
        elif command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
            admin systemctl start redis-server
        elif command -v service >/dev/null; then
            admin service redis-server start
        else
            die "No service manager detected. Start Redis manually and rerun."
        fi
    fi
    local attempt
    for attempt in 1 2 3 4 5; do
        reply="$(redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null || true)"
        if "$pg_bindir/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && [[ "$reply" == PONG || "$reply" == *NOAUTH* || "$reply" == *WRONGPASS* ]]; then
            log "PostgreSQL accepts connections on 127.0.0.1:5432; Redis responds on 127.0.0.1:6379"
            if [[ "$reply" != PONG ]]; then log "Redis requires authentication; existing access settings are preserved."; fi
            return
        fi
        sleep 1
    done
    die "Services are not ready on default local ports 5432/6379. Check service logs and port configuration."
}

if (( with_services )); then
    log "Preparing local PostgreSQL and Redis"
    discover_postgres
    if [[ -z "$pg_config_bin" ]]; then
        if [[ "$platform" == Darwin ]]; then
            run brew install postgresql@18
            pg_config_bin="$brew_prefix/opt/postgresql@18/bin/pg_config"
        else
            apt_install postgresql libpq-dev
            if (( ! dry_run )); then discover_postgres; else pg_config_bin=pg_config; fi
            [[ -n "$pg_config_bin" ]] || die "PostgreSQL installed but pg_config is missing; install its development package and set PG_CONFIG."
        fi
    fi
    if ! command -v redis-server >/dev/null || ! command -v redis-cli >/dev/null; then
        if [[ "$platform" == Darwin ]]; then run brew install redis; else apt_install redis-server redis-tools; fi
    fi
    configure_postgres
    start_and_check_services
    if (( ! dry_run )); then
        log "PostgreSQL is available; initialize the dedicated catalog separately."
    fi
fi

log "Verifying Python dependencies"
run "$venv_python" -c 'import fastapi, uvicorn, httpx, dotenv, sqlite3, psycopg, jsonschema; print("Backend and catalog imports OK")'
if (( with_reranker )); then
    run "$venv_python" -c 'import torch, sentence_transformers, huggingface_hub; print("Reranker libraries OK; model setup remains separate")'
fi
if (( with_dev )); then run "$venv_python" -c 'import pytest; print("Testing libraries OK")'; fi
run "$venv_python" -m pip check
if (( dry_run )); then
    log "Dry run complete. No packages installed, files written, or services started."
else
    log "Setup complete. Choose your model settings in .env separately."
fi
printf '\nRun the backend:\n  cd %q\n  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000\n' "$project_root"
