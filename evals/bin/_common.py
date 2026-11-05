"""Shared helpers for the agent-evals checkers. Standard library only (Python 3.11+)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HARBOR_VERSION = "0.23.0"
HARBOR_PYTHON = "3.12"

# Directories that never belong in a task, a build context or a snapshot.
JUNK_DIRS = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox",
    "node_modules", ".venv", "venv", ".git", ".hg", ".svn",
}

SECRET_PATTERNS = {
    "AWS access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{32,}"),
    "GitHub token": re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "Slack token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    "Stripe live key": re.compile(r"\bsk_live_[A-Za-z0-9]{20,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

TEXT_SCAN_LIMIT = 2 * 1024 * 1024  # skip larger files when scanning for secrets


@dataclass
class Report:
    """Collects every independent issue so the caller sees them all at once."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def extend(self, other: "Report", prefix: str = "") -> None:
        self.errors += [prefix + m for m in other.errors]
        self.warnings += [prefix + m for m in other.warnings]
        self.notes += [prefix + m for m in other.notes]

    def emit(self, title: str, as_json: bool = False) -> int:
        if as_json:
            print(json.dumps({"title": title, "ok": not self.errors, "errors": self.errors,
                              "warnings": self.warnings, "notes": self.notes}, indent=2))
        else:
            status = "FAIL" if self.errors else "PASS"
            print(f"{status}: {title}")
            for message in self.errors:
                print(f"  error: {message}")
            for message in self.warnings:
                print(f"  warning: {message}")
            for message in self.notes:
                print(f"  note: {message}")
        return 1 if self.errors else 0


ENV_EXAMPLE_NAMES = {".env.example", ".env.sample", ".env.template"}


def evals_root(explicit: str | None = None) -> Path:
    """The evals/ directory: --evals-dir, else the parent of bin/, else ./evals.

    The copy inside the plugin (skills/setup/evals/bin) is a template, never a project's evals/.
    """
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve().parent
    is_template = here.parent.parent.name == "setup" and here.parent.parent.parent.name == "skills"
    if here.name == "bin" and (here.parent / "README.md").exists() and not is_template:
        return here.parent
    return (Path.cwd() / "evals").resolve()


def test_key(task_dir: Path, evals_dir: Path) -> str:
    """A test's identity: its path relative to evals/ (tests/<id>, benchmarks/<id>/...), else its name."""
    task_dir = task_dir.resolve()
    if task_dir.is_relative_to(evals_dir):
        return task_dir.relative_to(evals_dir).as_posix()
    return task_dir.name


def resolve_task_dir(raw: str, evals_dir: Path, job_dir: Path | None = None) -> Path | None:
    """Find the task directory a Harbor trial ran. Harbor records -p exactly as typed, so a relative
    path is tried against the directory Harbor ran in (inferred from the job's jobs_dir), the current
    directory, the repository root and evals/. Returns None when no candidate holds a task.toml."""
    if not raw:
        return None
    path = Path(raw).expanduser()
    candidates = [path] if path.is_absolute() else []
    if not path.is_absolute():
        roots = []
        if job_dir is not None:
            config = job_dir / "config.json"
            try:
                jobs_dir = load_json(config).get("jobs_dir") if config.is_file() else None
            except (OSError, json.JSONDecodeError):
                jobs_dir = None
            if jobs_dir and not Path(jobs_dir).is_absolute():
                parts = [p for p in Path(jobs_dir).parts if p != "."]
                base = job_dir.resolve().parent
                if not parts or list(base.parts[-len(parts):]) == parts:
                    roots.append(Path(*base.parts[:len(base.parts) - len(parts)]))
        roots += [Path.cwd(), evals_dir.parent, evals_dir]
        candidates += [root / path for root in roots]
        candidates += [evals_dir / "tests" / path.name]
    for candidate in candidates:
        if (candidate / "task.toml").is_file():
            return candidate.resolve()
    return None


def repo_root(evals_dir: Path) -> Path:
    return evals_dir.parent


def load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def evals_config(evals_dir: Path) -> dict:
    path = evals_dir / "evals.toml"
    return load_toml(path) if path.exists() else {}


def secret_values(evals_dir: Path) -> dict[str, str]:
    """Values from evals/.env, used only to detect and redact them. Never print these."""
    path = evals_dir / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if len(value) >= 8:
            values[name.strip().removeprefix("export ").strip()] = value
    return values


def redact(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace(value, f"[redacted {name}]")
    for label, pattern in SECRET_PATTERNS.items():
        text = pattern.sub(f"[redacted {label}]", text)
    return text


def expand_globs(root: Path, patterns: list[str]) -> list[Path]:
    """Files matching gitignore-like patterns; a trailing ** or a directory means everything under it.
    (Path.glob('x/**') yields only directories before Python 3.13.)"""
    files: set[Path] = set()
    for pattern in patterns:
        pattern = pattern.strip().lstrip("/")
        if pattern.endswith("**"):
            pattern += "/*"
        for match in root.glob(pattern):
            if match.is_dir():
                files.update(p for p in match.rglob("*") if p.is_file())
            elif match.is_file():
                files.add(match)
    return sorted(files)


def walk_files(root: Path, skip_dirs: set[str] = frozenset()):
    """Yield files under root, not descending into skip_dirs."""
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in sorted(files):
            yield Path(current) / name


def scan_secrets(paths, values: dict[str, str], base: Path, report: Report) -> None:
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > TEXT_SCAN_LIMIT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(base) if path.is_relative_to(base) else path
        for name, value in values.items():
            if value in text:
                report.error(f"{rel} contains the value of {name} from evals/.env")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                report.error(f"{rel} contains what looks like a {label}")


def dir_digest(root: Path, exclude_names: set[str] = frozenset({"README.md"})) -> str:
    """sha256 over sorted relative paths and file bytes, skipping junk dirs and exclude_names at the top level."""
    digest = hashlib.sha256()
    for path in walk_files(root, JUNK_DIRS):
        rel = path.relative_to(root).as_posix()
        if rel in exclude_names:
            continue
        digest.update(rel.encode() + b"\0")
        digest.update(path.read_bytes() if not path.is_symlink() else os.readlink(path).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_state(evals_dir: Path) -> dict:
    path = evals_dir / "frozen.json"
    return load_json(path) if path.exists() else {"tests": {}}


def declared_evaluators(task_toml: dict) -> list[dict]:
    return list(task_toml.get("metadata", {}).get("evals", {}).get("evaluators", []) or [])


def normalized(score: float, low: float, high: float) -> float:
    if high == low:
        return 1.0 if score >= high else 0.0
    return min(1.0, max(0.0, (score - low) / (high - low)))


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)
