"""Local CI runner. No hosted CI: run this before pushing or merging.

    python tools/ci.py                  # full pipeline (needs Docker for PostgreSQL)
    python tools/ci.py --quick          # lint + SQLite tests (no Docker, no network)
    python tools/ci.py --only lint test-sqlite
    python tools/ci.py --skip audit     # e.g. when offline
    python tools/ci.py --with proxy-verify   # also run the real egress proxy checks
    python tools/ci.py --list
    python tools/ci.py --install-hook   # git pre-push hook that runs --quick

Uses `backend/.venv` (created with the dev extras when missing). Every database is
disposable: PostgreSQL runs in a throwaway, memory-backed container with a random password
and port, and is removed even when a stage fails. Only the standard library is used.
"""

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PROXY_DIR = ROOT / "deploy" / "egress-proxy"
VENV = BACKEND / ".venv"
POSTGRES_IMAGE = "postgres:17-alpine"
PYTHON_IMAGE = "python:3.14.10-slim"
TEST_ARTIFACT_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="  # 32 zero bytes, test only


class StageFailed(Exception):
    pass


@dataclass
class Context:
    verbose: bool
    python: Path = field(default_factory=Path)
    pg_url: str | None = None

    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path = ROOT,
        env: dict[str, str] | None = None,
        check: bool = True,
        quiet: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in args]
        if self.verbose:
            print(f"  $ {' '.join(command)}", flush=True)
        result = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=not self.verbose,
        )
        if check and result.returncode != 0:
            if not self.verbose and not quiet:
                output = (result.stdout or "") + (result.stderr or "")
                print("\n".join(output.rstrip().splitlines()[-40:]))
            raise StageFailed(f"{command[0]} exited with {result.returncode}")
        return result


def _venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise StageFailed(f"`{name}` is not installed or not on PATH")
    return found


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _test_env(ctx: Context, **extra: str) -> dict[str, str]:
    env = {
        "APP_ENV": "test",
        "SESSION_SECRET": "ci-session-secret-that-is-long-enough-0123456789",
        "ARTIFACT_ENCRYPTION_KEY": TEST_ARTIFACT_KEY,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


# --- stages ---------------------------------------------------------------------------------


def stage_setup(ctx: Context) -> str:
    python = _venv_python()
    if not python.exists():
        ctx.run([sys.executable, "-m", "venv", VENV])
        ctx.run([python, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    ctx.run([python, "-m", "pip", "install", "-q", "-e", ".[dev]"], cwd=BACKEND)
    # Chromium for the Phase 6 sandbox adapter browser tests (no-op when already present).
    ctx.run([python, "-m", "playwright", "install", "chromium"], cwd=BACKEND)
    ctx.python = python
    version = ctx.run([python, "--version"]).stdout.strip()
    return f"{version} in backend/.venv"


def stage_lint(ctx: Context) -> str:
    ctx.run([ctx.python, "-m", "ruff", "check", "app", "migrations", "tests", "sandbox_ats"],
            cwd=BACKEND)
    ctx.run([ctx.python, "-m", "mypy", "app", "sandbox_ats"], cwd=BACKEND)
    ctx.run([ctx.python, "-m", "compileall", "-q", "app", "migrations", "tests", "sandbox_ats"],
            cwd=BACKEND)
    return "ruff, mypy, compileall"


def _pytest(ctx: Context, env: dict[str, str]) -> str:
    result = ctx.run(
        [ctx.python, "-m", "pytest", "-q", "-p", "no:warnings", "-o",
         "addopts=--strict-markers"],
        cwd=BACKEND,
        env=env,
    )
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[-1].strip("= ") if lines else "passed"


def stage_test_sqlite(ctx: Context) -> str:
    return _pytest(ctx, _test_env(ctx, TEST_DATABASE_URL="sqlite+pysqlite:///:memory:"))


def stage_test_postgres(ctx: Context) -> str:
    docker = _tool("docker")
    password = secrets.token_hex(16)
    port = _free_port()
    name = f"jfa-ci-pg-{secrets.token_hex(4)}"
    ctx.run([
        docker, "run", "-d", "--rm", "--name", name,
        "-e", "POSTGRES_DB=jfa_ci", "-e", "POSTGRES_USER=jfa_ci",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-p", f"127.0.0.1:{port}:5432", "--tmpfs", "/var/lib/postgresql/data",
        POSTGRES_IMAGE,
    ])
    try:
        deadline = time.monotonic() + 60
        while True:
            ready = ctx.run(
                [docker, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "jfa_ci",
                 "-d", "jfa_ci"],
                check=False,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise StageFailed("PostgreSQL did not become ready within 60 s")
            time.sleep(1)
        url = f"postgresql+psycopg://jfa_ci:{password}@127.0.0.1:{port}/jfa_ci"
        summary = _pytest(ctx, _test_env(ctx, TEST_DATABASE_URL=url))
        # Migration round trip and model/migration drift on a clean schema.
        ctx.run([docker, "exec", name, "psql", "-U", "jfa_ci", "-d", "jfa_ci", "-qc",
                 "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"])
        env = _test_env(ctx, DATABASE_URL=url,
                        ARTIFACT_STORAGE_PATH=str(BACKEND / "tests" / ".artifacts-ci"))
        for step in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"],
                     ["check"]):
            ctx.run([ctx.python, "-m", "alembic", *step], cwd=BACKEND, env=env)
        version = ctx.run([docker, "exec", name, "postgres", "--version"]).stdout.split()[-1]
        return f"PostgreSQL {version}: {summary}; alembic round trip + check clean"
    finally:
        ctx.run([docker, "rm", "-f", name], check=False, quiet=True)
        shutil.rmtree(BACKEND / "tests" / ".artifacts-ci", ignore_errors=True)


def stage_audit(ctx: Context) -> str:
    result = ctx.run(
        [ctx.python, "-m", "pip_audit", "--progress-spinner", "off", "--skip-editable"],
        cwd=BACKEND,
    )
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    verdict = [line for line in output.splitlines() if "vulnerabilit" in line.lower()]
    return verdict[0].strip() if verdict else "no vulnerabilities reported"


def stage_frontend(ctx: Context) -> str:
    npm = _tool("npm")
    if not (FRONTEND / "node_modules").exists():
        ctx.run([npm, "ci", "--no-audit", "--no-fund"], cwd=FRONTEND)
    ctx.run([npm, "run", "-s", "build"], cwd=FRONTEND)
    return "tsc + vite build"


def stage_images(ctx: Context) -> str:
    docker = _tool("docker")
    ctx.run([docker, "build", "-q", "-t", "jobfinderagent-backend:ci", BACKEND])
    ctx.run([docker, "build", "-q", "-t", "jobfinderagent-egress-proxy:ci", PROXY_DIR])
    return "backend and egress-proxy images build"


def stage_proxy_verify(ctx: Context) -> str:
    """Real Squid in an isolated network; needs internet access (example.com, iana.org)."""
    docker = _tool("docker")
    suffix = secrets.token_hex(4)
    internal, egress, proxy = f"jfa-ci-int-{suffix}", f"jfa-ci-egr-{suffix}", f"jfa-ci-px-{suffix}"
    subnet = "10.253.0.0/24"
    ctx.run([docker, "build", "-q", "-t", "jobfinderagent-egress-proxy:ci", PROXY_DIR])
    try:
        ctx.run([docker, "network", "create", "--internal", "--subnet", subnet, internal])
        ctx.run([docker, "network", "create", egress])
        ctx.run([docker, "run", "-d", "--rm", "--name", proxy, "--network", internal,
                 "-e", "EGRESS_ALLOWLIST=example.com,*.iana.org,localtest.me",
                 "-e", f"EGRESS_CLIENT_CIDR={subnet}", "jobfinderagent-egress-proxy:ci"])
        ctx.run([docker, "network", "connect", egress, proxy])
        time.sleep(3)
        result = ctx.run([
            docker, "run", "--rm", "--network", internal,
            "-v", f"{PROXY_DIR}:/verify:ro", PYTHON_IMAGE,
            "python", "-I", "/verify/verify.py", "--proxy", f"{proxy}:3128",
            "--allowed-host", "example.com", "--denied-host", "www.example.com",
            "--private-host", "localtest.me", "--wildcard-apex", "iana.org",
        ])
        return (result.stdout or "").strip().splitlines()[-1]
    finally:
        ctx.run([docker, "rm", "-f", proxy], check=False, quiet=True)
        ctx.run([docker, "network", "rm", internal, egress], check=False, quiet=True)


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[Context], str]
    description: str
    default: bool = True
    quick: bool = False


STAGES: tuple[Stage, ...] = (
    Stage("setup", stage_setup, "backend/.venv with dev extras", quick=True),
    Stage("lint", stage_lint, "ruff, mypy, compileall", quick=True),
    Stage("test-sqlite", stage_test_sqlite, "pytest on in-memory SQLite", quick=True),
    Stage("test-postgres", stage_test_postgres,
          "pytest + Alembic round trip + drift check on disposable PostgreSQL (Docker)"),
    Stage("audit", stage_audit, "pip-audit of the backend environment (network)"),
    Stage("frontend", stage_frontend, "frontend type check and production build"),
    Stage("images", stage_images, "build backend and egress-proxy images (Docker)"),
    Stage("proxy-verify", stage_proxy_verify,
          "real egress proxy enforcement checks (Docker + network)", default=False),
)


def _install_hook() -> int:
    hooks = Path(subprocess.check_output(
        ["git", "rev-parse", "--git-path", "hooks"], cwd=ROOT, text=True
    ).strip())
    hook = (ROOT / hooks if not hooks.is_absolute() else hooks) / "pre-push"
    if hook.exists() and "tools/ci.py" not in hook.read_text(encoding="utf-8", errors="ignore"):
        print(f"error: {hook} exists and was not written by tools/ci.py; not overwriting")
        return 1
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n# Installed by tools/ci.py --install-hook. Skip once with: git push --no-verify\n"
        'exec python "$(git rev-parse --show-toplevel)/tools/ci.py" --quick\n',
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    print(f"installed {hook} (runs `tools/ci.py --quick` before each push)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/ci.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    names = [stage.name for stage in STAGES]
    parser.add_argument("--quick", action="store_true", help="setup, lint, test-sqlite only")
    parser.add_argument("--only", nargs="+", choices=names, metavar="STAGE")
    parser.add_argument("--skip", nargs="+", choices=names, metavar="STAGE", default=[])
    parser.add_argument("--with", dest="extra", nargs="+", choices=names, metavar="STAGE",
                        default=[], help="add opt-in stages such as proxy-verify")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="stream command output")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--install-hook", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for stage in STAGES:
            flags = "quick" if stage.quick else ("default" if stage.default else "opt-in")
            print(f"{stage.name:14} [{flags:7}] {stage.description}")
        return 0
    if args.install_hook:
        return _install_hook()

    if args.only:
        selected = [s for s in STAGES if s.name in args.only or s.name == "setup"]
    elif args.quick:
        selected = [s for s in STAGES if s.quick]
    else:
        selected = [s for s in STAGES if s.default or s.name in args.extra]
    selected = [s for s in selected if s.name not in args.skip or s.name == "setup"]

    ctx = Context(verbose=args.verbose, python=_venv_python())
    results: list[tuple[str, bool, float, str]] = []
    for stage in selected:
        print(f"\n=== {stage.name}: {stage.description}", flush=True)
        started = time.monotonic()
        try:
            detail = stage.run(ctx)
            ok = True
        except StageFailed as exc:
            detail, ok = str(exc), False
        except KeyboardInterrupt:
            print("\ninterrupted")
            return 130
        elapsed = time.monotonic() - started
        results.append((stage.name, ok, elapsed, detail))
        print(f"--- {'PASS' if ok else 'FAIL'} {stage.name} ({elapsed:.1f}s): {detail}", flush=True)
        if not ok and (args.fail_fast or stage.name == "setup"):
            break

    print("\n=== summary")
    for name, ok, elapsed, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:14} {elapsed:6.1f}s  {detail}")
    failed = [name for name, ok, _, _ in results if not ok]
    print(f"\n{'CI FAILED: ' + ', '.join(failed) if failed else 'CI PASSED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
