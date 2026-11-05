#!/usr/bin/env python3
"""Check the EvalsWiki in evals/wiki/ and the pages tests cite.

Usage:
  python3 evals/bin/check_wiki.py [--evals-dir evals] [--json]

Pages live in evals/wiki/{requirements,risks,open-questions}/<id>.md; a page's id is its
file name. Checks: ids are lowercase slugs and unique across the three folders, each page
starts with a '# ' title, relative Markdown links resolve, and every id a test or
benchmark lists in [metadata.evals].covers exists. Read-only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Report, evals_root, load_toml  # noqa: E402

KINDS = ("requirements", "risks", "open-questions")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(#[^)]*)?\)")
RISK_FIELDS = ("mechanism", "trigger", "observable failure")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evals-dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    evals_dir = evals_root(args.evals_dir)
    wiki = evals_dir / "wiki"
    report = Report()
    pages: dict[str, Path] = {}
    if not wiki.is_dir():
        report.note("evals/wiki/ does not exist yet")
    for kind in KINDS:
        folder = wiki / kind
        if not folder.is_dir():
            continue
        for page in sorted(folder.glob("*.md")):
            page_id = page.stem
            rel = page.relative_to(evals_dir)
            if not ID_RE.match(page_id):
                report.error(f"{rel}: the file name is the page id and must be a lowercase slug")
            if page_id in pages:
                report.error(f"{rel}: id {page_id!r} is also used by {pages[page_id].relative_to(evals_dir)}")
            pages[page_id] = page
            text = page.read_text(encoding="utf-8", errors="replace")
            first = next((line for line in text.splitlines() if line.strip()), "")
            if not first.startswith("# "):
                report.error(f"{rel}: the first line must be a '# ' title")
            if kind == "risks":
                lowered = text.lower()
                missing = [f for f in RISK_FIELDS if f not in lowered]
                if missing:
                    report.warn(f"{rel}: a risk should state its {', '.join(missing)}")
            for target, _anchor in LINK_RE.findall(text):
                if "://" in target:
                    continue
                if not (page.parent / target).resolve().is_file():
                    report.error(f"{rel}: link to {target} does not resolve")
        for other in sorted(p for p in folder.iterdir() if p.suffix != ".md" and not p.name.startswith(".")):
            report.warn(f"{other.relative_to(evals_dir)}: only .md pages belong in {kind}/")

    for kind in ("tests", "benchmarks"):
        base = evals_dir / kind
        if not base.is_dir():
            continue
        for toml_path in sorted(base.rglob("task.toml")):  # benchmark rows sit at benchmarks/<id>/rows/<n>/
            try:
                covers = load_toml(toml_path).get("metadata", {}).get("evals", {}).get("covers", []) or []
            except Exception as exc:
                report.error(f"{toml_path.relative_to(evals_dir)} does not parse: {exc}")
                continue
            for ref in covers:
                if ref not in pages:
                    report.error(f"{toml_path.relative_to(evals_dir)} covers {ref!r}, which is not a wiki page")
    report.note(f"{len(pages)} wiki pages")
    return report.emit("wiki", args.json)


if __name__ == "__main__":
    sys.exit(main())
