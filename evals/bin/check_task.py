#!/usr/bin/env python3
"""Check Harbor test tasks against the agent-evals contract (see evals/README.md).

Usage:
  python3 evals/bin/check_task.py evals/tests/<id> [more task dirs] [--verifier [--fixture DIR]] [--json]

Each run also checks evals.toml [harbor], and that every task builds in the cloud (fixed rule 8): each
FROM or COPY --from in its Dockerfiles names the harness image from [harbor.image], as the only FROM and
the first instruction, or an image its registry serves without credentials (asked anonymously with HEAD
requests, as a cloud builder would pull it), and no FROM reads a build argument or pins a platform other
than linux/amd64. With --json, each report is one JSON document: the [harbor] table first, then each task.

Static checks always run, including Harbor's own task loading (through `uv`; --no-harbor skips
it). --verifier also builds the task's verifier and runs tests/test.sh, as Harbor would, on a
synthetic normal trajectory and a synthetic empty-reply trajectory, requiring a score and feedback
from every declared code evaluator (needs Docker). --fixture DIR copies DIR's files into
/logs/agent beside the synthetic trajectory, for evaluators that also read state the harness
exports there (a database dump, a mock journal). Writes nothing in the repository.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ENV_EXAMPLE_NAMES, HARBOR_PYTHON, HARBOR_VERSION, JUNK_DIRS, Report, declared_evaluators, dir_digest,
    evals_config, evals_root, expand_globs, file_sha256, frozen_state, load_toml, normalized, repo_root,
    scan_secrets, secret_values, test_key, walk_files,
)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
COPY_RE = re.compile(r"^\s*(COPY|ADD)\s+(.*)$", re.IGNORECASE | re.MULTILINE)
TRAJECTORY = "/logs/agent/trajectory.json"
HARBOR_LOAD = r"""
import json, sys
from pathlib import Path
from harbor.models.task.task import Task
out = {}
for raw in sys.argv[1:]:
    path = Path(raw)
    try:
        if not Task.is_valid_dir(path):
            out[raw] = "Harbor does not recognize this directory as a task (it would skip it without a message)"
            continue
        Task(path)
        out[raw] = None
    except Exception as exc:
        out[raw] = f"{type(exc).__name__}: {exc}"[:1500]
print(json.dumps(out))
"""


def is_separate(config: dict) -> bool:
    verifier = config.get("verifier", {}) or {}
    return verifier.get("environment_mode") == "separate" or "environment" in verifier


def artifact_sources(config: dict) -> list[str]:
    sources = []
    for entry in config.get("artifacts", []) or []:
        sources.append(entry if isinstance(entry, str) else (entry or {}).get("source", ""))
    return sources


def host_templates(value, path: str, report: Report) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            host_templates(child, f"{path}.{key}" if path else key, report)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            host_templates(child, f"{path}[{index}]", report)
    elif isinstance(value, str) and "${" in value:
        report.error(f"task.toml {path} reads a host variable (${{...}}); Harbor would copy a "
                     "host value into a container. Pass values another way")


def check_evaluators(task: Path, config: dict, report: Report) -> list[dict]:
    meta = config.get("metadata", {}).get("evals")
    if not isinstance(meta, dict):
        report.error("task.toml has no [metadata.evals] table")
        return []
    if meta.get("id") != task.name:
        report.error(f"[metadata.evals].id is {meta.get('id')!r}; it must equal the directory name {task.name!r}")
    evaluators = declared_evaluators(config)
    if not evaluators:
        report.error("declares no [[metadata.evals.evaluators]]; a test must be scored")
    seen = set()
    for index, ev in enumerate(evaluators):
        where = f"evaluator #{index + 1} ({ev.get('id', '?')})"
        ev_id = ev.get("id")
        if not isinstance(ev_id, str) or not ID_RE.match(ev_id):
            report.error(f"{where}: id must be a lowercase slug")
        elif ev_id in seen:
            report.error(f"{where}: duplicate id")
        seen.add(ev_id)
        kind = ev.get("type")
        if kind not in ("code", "judge"):
            report.error(f"{where}: type must be 'code' or 'judge'")
        low, high = ev.get("min_score", 0.0), ev.get("max_score", 1.0)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (low, high)) or low >= high:
            report.error(f"{where}: min_score and max_score must be finite numbers with min < max")
        method = ev.get("method")
        if method not in ("exact", "semantic"):
            report.error(f"{where}: method must be 'exact' (code can decide it) or 'semantic' (open-ended meaning)")
        elif method == "semantic" and kind != "judge":
            report.error(f"{where}: a semantic criterion needs a judge; keywords and regexes miss valid phrasings")
        if not str(ev.get("criterion", "")).strip():
            report.error(f"{where}: criterion (one sentence on what earns full marks) is missing")
        if kind == "judge":
            rubric = ev.get("rubric")
            if not rubric or not (task / rubric).is_file():
                report.error(f"{where}: rubric {rubric!r} not found; the path is relative to the test directory")
    for ref in meta.get("covers", []) or []:
        if not isinstance(ref, str):
            report.error("[metadata.evals].covers must list wiki page ids")
    return evaluators


def check_static(task: Path, evals_dir: Path, report: Report) -> dict:
    toml_path = task / "task.toml"
    if not toml_path.is_file():
        report.error("task.toml not found")
        return {}
    try:
        config = load_toml(toml_path)
    except Exception as exc:  # tomllib raises TOMLDecodeError
        report.error(f"task.toml does not parse: {exc}")
        return {}

    if not (task / "instruction.md").is_file():
        report.error("instruction.md is missing (Harbor requires it, even empty)")
    env = config.get("environment", {}) or {}
    if not (task / "environment").is_dir():
        report.error("environment/ is missing (Harbor skips a task without it, even with docker_image set)")
    elif not ((task / "environment" / "Dockerfile").is_file()
              or (task / "environment" / "docker-compose.yaml").is_file() or env.get("docker_image")):
        report.error("environment/ has no Dockerfile or docker-compose.yaml and [environment].docker_image is unset")
    if not (task / "tests" / "test.sh").is_file():
        report.error("tests/test.sh is missing")
    info = config.get("task")
    if isinstance(info, dict) and not TASK_NAME_RE.match(str(info.get("name", ""))):
        report.error("[task].name must look like 'org/name' (Harbor skips the task otherwise)")

    if is_separate(config):
        verifier_env = (config.get("verifier", {}) or {}).get("environment", {}) or {}
        if not (task / "tests" / "Dockerfile").is_file() and not verifier_env.get("docker_image"):
            report.error("separate verifier needs tests/Dockerfile or [verifier.environment].docker_image")
        dockerfile = task / "tests" / "Dockerfile"
        if dockerfile.is_file() and "/tests" not in dockerfile.read_text(errors="ignore"):
            report.warn("tests/Dockerfile never mentions /tests; Harbor does not upload tests/ to a "
                        "separate verifier, so the image must COPY them to /tests")
        if TRAJECTORY not in artifact_sources(config):
            report.error(f"separate verifier: add {TRAJECTORY!r} to top-level artifacts, or the verifier never sees the trajectory")

    for table in ("environment", "verifier", "solution", "agent"):
        host_templates(config.get(table, {}), table, report)

    check_evaluators(task, config, report)

    for current, dirs, _files in os.walk(task):
        for name in sorted(d for d in dirs if d in JUNK_DIRS):
            rel = (Path(current) / name).relative_to(task)
            report.error(f"{rel} is a cache or dependency folder; remove it (it breaks or bloats image builds)")
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
    for path in walk_files(task, JUNK_DIRS):
        rel = path.relative_to(task)
        name = rel.name
        if name == ".env" or (name.startswith(".env.") and name not in ENV_EXAMPLE_NAMES):
            report.error(f"{rel} is an env file; secret values never go in a task")
        if path.is_symlink():
            report.error(f"{rel} is a symlink; Harbor build contexts need regular files")

    values = secret_values(evals_dir)
    scan_secrets(walk_files(task, JUNK_DIRS), values, task, report)
    harness = evals_dir / "harness"
    if harness.is_dir():
        scan_secrets(walk_files(harness, JUNK_DIRS), values, evals_dir, report)
    check_live_copies(task, evals_dir, report)
    check_cloud_build(task, config, evals_dir, report)

    frozen = frozen_state(evals_dir).get("tests", {}).get(test_key(task, evals_dir))
    if frozen and frozen.get("digest") != dir_digest(task):
        report.warn("changed since it was frozen; a change needs a reason (freeze.py --reason) and a fresh review")
    return config


def check_live_copies(task: Path, evals_dir: Path, report: Report) -> None:
    """Files whose edits change the agent's behavior must not be baked into an image."""
    patterns = (evals_config(evals_dir).get("target", {}) or {}).get("live_paths", []) or []
    if not patterns:
        report.note("evals.toml [target].live_paths is empty, so copies of prompts or configs were not checked")
        return
    root = repo_root(evals_dir)
    live = {file_sha256(p): p.relative_to(root).as_posix()
            for p in expand_globs(root, patterns) if not p.is_relative_to(evals_dir) and p.stat().st_size}
    if not live:
        report.warn(f"live_paths {patterns} match no files")
        return
    live_paths = set(live.values())
    for base in (task / "environment", evals_dir / "harness"):
        if not base.is_dir():
            continue
        for path in walk_files(base, JUNK_DIRS):
            if path.stat().st_size and (match := live.get(file_sha256(path))):
                report.error(f"{path.relative_to(root)} is a copy of live file {match}; trials must read it "
                             "from the current source, or optimizer edits are never measured")
            if path.name.startswith("Dockerfile"):
                for _verb, args in COPY_RE.findall(path.read_text(errors="ignore")):
                    sources = [a for a in args.split() if not a.startswith("--")][:-1]
                    for source in sources:
                        source = source.strip('"[],').lstrip("./") or "."
                        hits = [p for p in live_paths if source == "." or p == source or p.startswith(source + "/")]
                        if hits:
                            report.warn(f"{path.relative_to(root)} copies {source!r}, which includes live file "
                                        f"{hits[0]}; fine if the trial still runs it from the current source, "
                                        "but a baked-in copy hides an optimizer's edits")


def harbor_table(evals_dir: Path) -> dict:
    harbor = evals_config(evals_dir).get("harbor")
    return harbor if isinstance(harbor, dict) else {}


def harness_tag(evals_dir: Path) -> str | None:
    image = harbor_table(evals_dir).get("image")
    tag = image.get("tag") if isinstance(image, dict) else None
    return tag if isinstance(tag, str) and tag else None


def has_judges(tasks: list[Path]) -> bool:
    """Whether any of these tasks declares a judge evaluator (unreadable task.toml files count as none)."""
    for task in tasks:
        try:
            if any(ev.get("type") == "judge" for ev in declared_evaluators(load_toml(task / "task.toml"))):
                return True
        except Exception:  # check_static reports the parse error
            continue
    return False


def check_harbor(evals_dir: Path, report: Report, judged: bool = False) -> None:
    """evals.toml [harbor]: everything a runner needs to run the suite without knowing the agent.
    judged: whether the tests being checked declare judge evaluators."""
    config = evals_config(evals_dir)
    if (evals_dir / "evals.toml").is_file():
        scan_secrets([evals_dir / "evals.toml"], secret_values(evals_dir), evals_dir, report)
    harbor = config.get("harbor")
    if harbor is None:
        report.warn("evals.toml has no [harbor] table, so no hosted runner can run these tests "
                    "(see the evals.toml contract in evals/README.md)")
        return
    if not isinstance(harbor, dict):
        report.error("[harbor] must be a table")
        return
    root = repo_root(evals_dir)

    def inside(rel) -> bool:  # a clean clone holds only what is under the repository root
        return isinstance(rel, str) and (root / rel).resolve().is_relative_to(root.resolve())

    paths = harbor.get("pythonpath", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        report.error("[harbor].pythonpath must list directories relative to the repository root")
        paths = []
    for rel in paths:
        if not inside(rel) or not (root / rel).is_dir():
            report.error(f"[harbor].pythonpath {rel!r} is not a directory under the repository root")
    agent = harbor.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        report.error("[harbor].agent must name the agent passed to `harbor run -a` (module:Class for a custom class)")
    elif ":" in agent:
        module = agent.split(":", 1)[0].replace(".", "/")
        if not any((root / rel / f"{module}.py").is_file() or (root / rel / module / "__init__.py").is_file()
                   for rel in paths):
            report.error(f"[harbor].agent {agent!r}: no {module}.py under any [harbor].pythonpath directory, "
                         "so Harbor cannot import it")
    options = harbor.get("agent_env", {})
    runtime = config.get("runtime")
    names = runtime.get("env") if isinstance(runtime, dict) else None
    secrets = {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()
    if not isinstance(options, dict) or not all(isinstance(v, str) for v in options.values()):
        report.error("[harbor].agent_env must map option names to strings (each passed as --ae NAME=value)")
    else:
        for name, value in options.items():
            if name in secrets:
                report.error(f"[harbor].agent_env sets {name}, a secret named in [runtime].env; secret values "
                             "reach Harbor from the runner's environment, never from evals.toml")
            if "${" in value:
                report.error(f"[harbor].agent_env {name} reads a host variable (${{...}}); Harbor would copy a "
                             "host value into the container")
    mark = harbor.get("pass_mark")
    if mark is None:
        report.error("[harbor].pass_mark is missing: the score from 0 to 1 at or above which a test passes. "
                     "It is the user's choice; there is no default")
    elif isinstance(mark, bool) or not isinstance(mark, (int, float)) or not 0 <= mark <= 1:
        report.error("[harbor].pass_mark must be a number from 0 to 1")
    judge = config.get("judge")
    if judged and isinstance(judge, dict) and judge.get("mode") == "subagent":
        report.warn('[judge].mode is "subagent", but these tests have judge evaluators: a hosted runner has no '
                    'coding agent to judge with, so they stay "judges pending" and never pass there. A hosted '
                    'runner needs mode = "api"')
    image = harbor.get("image")
    if not isinstance(image, dict):
        report.error("[harbor.image] is missing: the tag, dockerfile and context of the harness image tasks start FROM")
        return
    tag, dockerfile, context = image.get("tag"), image.get("dockerfile"), image.get("context")
    if not isinstance(tag, str) or not tag or "$" in tag or any(c.isspace() for c in tag):
        report.error("[harbor.image].tag must be the image name task Dockerfiles use in FROM")
    if not inside(dockerfile) or not (root / dockerfile).is_file():
        report.error(f"[harbor.image].dockerfile {dockerfile!r} is not a file under the repository root")
    else:
        check_dockerfile((root / dockerfile).read_text(errors="ignore"), f"[harbor.image] {dockerfile}", None, report)
    if not inside(context) or not (root / context).is_dir():
        report.error(f"[harbor.image].context {context!r} is not a directory under the repository root")
    elif isinstance(dockerfile, str):
        context_dir, recipe = root / context, root / dockerfile
        # The ignore files both Docker (BuildKit) and Modal read.
        ignores = (recipe.with_name(recipe.name + ".dockerignore"), context_dir / ".dockerignore")
        if any(p.is_file() for p in ignores):
            return
        junk = []
        for current, dirs, _files in os.walk(context_dir):
            junk += [(Path(current) / d).relative_to(context_dir).as_posix() for d in sorted(dirs) if d in JUNK_DIRS]
            dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
        if junk:
            report.warn(f"[harbor.image].context holds {', '.join(junk[:3])}; add {recipe.name}.dockerignore beside "
                        "the Dockerfile (Docker and Modal both read it) or narrow the context, or every build "
                        "uploads them")


DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io"}
LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
CLOUD_PLATFORMS = {"linux/amd64", "$BUILDPLATFORM", "${BUILDPLATFORM}", "$TARGETPLATFORM", "${TARGETPLATFORM}"}
PARSER_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape|check)\s*=", re.IGNORECASE)
HEREDOC_RE = re.compile(r"<<-?([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))
REGISTRY_TIMEOUT = 10
_unreachable: set[str] = set()


def dockerfile_instructions(text: str) -> list[tuple[str, list[str]]]:
    """(INSTRUCTION, arguments) per Dockerfile instruction: continuation lines joined, comments and
    heredoc bodies (RUN <<EOF ... EOF) skipped."""
    instructions, pending, heredocs = [], "", []
    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip()
        if heredocs:
            if stripped == heredocs[0]:
                heredocs.pop(0)
            continue
        if stripped.startswith("#") or (not stripped and not pending):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        words, pending = (pending + stripped).split(), ""
        if words:
            instructions.append((words[0].upper(), words[1:]))
            if words[0].upper() in ("RUN", "COPY", "ADD"):
                heredocs = [match[1] for match in HEREDOC_RE.findall(" ".join(words[1:]))]
    if pending.split():
        words = pending.split()
        instructions.append((words[0].upper(), words[1:]))
    return instructions


def parser_directives(text: str) -> list[str]:
    """The parser directives (# syntax=, # escape=, # check=) at the top of a Dockerfile."""
    directives = []
    for line in text.lstrip("﻿").splitlines():
        if not PARSER_DIRECTIVE_RE.match(line.strip()):
            break
        directives.append(line.strip())
    return directives


def check_dockerfile(text: str, where: str, harness: str | None, report: Report) -> None:
    """Fixed rule 8: every image this Dockerfile starts from or reads from can be had in the cloud."""
    instructions = dockerfile_instructions(text)
    froms = [index for index, (op, _args) in enumerate(instructions) if op == "FROM"]
    stages: set[str] = set()
    for index, (op, args) in enumerate(instructions):
        if op == "FROM":
            options = [a for a in args if a.startswith("--")]
            words = [a for a in args if not a.startswith("--")]
            image = words[0] if words else ""
            platform = next((o.split("=", 1)[1] for o in options if o.startswith("--platform=")), None)
            if platform is not None and platform not in CLOUD_PLATFORMS:
                report.error(f"{where}: FROM {image} pins --platform={platform}; cloud builders build linux/amd64")
            if harness and image == harness:
                if froms != [0]:
                    report.error(f"{where}: a Dockerfile that starts FROM the harness image {harness} must have no "
                                 "other FROM and no instruction before it, so a runner can apply the rest of it to "
                                 "the harness it builds from [harbor.image]")
                if directives := parser_directives(text):
                    report.error(f"{where}: starts FROM the harness image but has parser directives "
                                 f"({', '.join(directives)}); a runner applies only the instructions after FROM, so "
                                 "their effect would be lost. Remove them")
            else:
                check_image(image, f"{where}: FROM {image}", stages, harness, report)
            if len(words) >= 3 and words[1].upper() == "AS":
                stages.add(words[2].lower())
        elif op in ("COPY", "ADD", "RUN"):
            for option in args:
                if not option.startswith("--"):
                    break  # options come before sources and commands
                if option.startswith("--from=") and op != "RUN":
                    source, label = option.split("=", 1)[1], f"{where}: {op} {option}"
                elif option.startswith("--mount="):
                    fields = dict(f.split("=", 1) for f in option.split("=", 1)[1].split(",") if "=" in f)
                    source, label = fields.get("from", ""), f"{where}: RUN --mount from={fields.get('from')}"
                else:
                    continue
                if source and source == harness:
                    report.error(f"{label} reads from the harness image, which a cloud builder has only as the "
                                 "base a runner applies this Dockerfile to; start FROM it instead")
                elif source and not source.isdigit():
                    check_image(source, label, stages, harness, report)


def local_registry(registry: str) -> bool:
    """True for a registry on this machine or its private network, which a cloud builder cannot reach."""
    host = (registry if registry.endswith("]") else registry.rsplit(":", 1)[0]).strip("[]").lower()
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def check_image(image: str, label: str, stages: set[str], harness: str | None, report: Report) -> None:
    if not image:
        report.error(f"{label} names no image")
        return
    if "$" in image:
        report.error(f"{label} reads a build argument; cloud builds get none, so name the image directly")
        return
    if image.lower() in stages or image == "scratch":
        return
    registry = image_reference(image)[0]
    if local_registry(registry):
        report.error(f"{label} comes from a registry on this machine or its network ({registry}), which a cloud "
                     "builder cannot reach")
        return
    found = pullable(image)
    if found is False:
        fix = (f"start FROM the harness image {harness} (built from [harbor.image])" if harness else
               "declare it as the harness image in evals.toml [harbor.image]")
        report.error(f"{label}: no registry serves this image without credentials, so a cloud builder cannot "
                     f"pull it (a tag that exists only where it was built, or a private image). Either {fix}, "
                     "or use an image a registry serves publicly")
    elif found is None:
        report.note(f"{label}: its registry could not be reached, so it was not checked for cloud builds")


def image_reference(image: str) -> tuple[str, str, str]:
    """(registry, repository, tag or digest), resolved the way Docker resolves a FROM name."""
    name, _, digest = image.partition("@")
    first, slash, rest = name.partition("/")
    registry, repository = (first, rest) if slash and ("." in first or ":" in first or first == "localhost") \
        else ("docker.io", name)
    tag = ""
    if ":" in repository.rsplit("/", 1)[-1]:
        repository, tag = repository.rsplit(":", 1)
    if registry in DOCKER_HUB and "/" not in repository:
        repository = "library/" + repository
    return registry, repository, digest or tag or "latest"


@functools.lru_cache(maxsize=None)
def pullable(image: str) -> bool | None:
    """Whether the image's registry serves it to anyone, as a cloud builder with no credentials pulls it.
    None when the registry could not be reached. Sends only HEAD requests, which Docker Hub does not count as pulls."""
    registry, repository, reference = image_reference(image)
    host = "registry-1.docker.io" if registry in DOCKER_HUB else registry
    if host in _unreachable:
        return None
    url = f"https://{host}/v2/{repository}/manifests/{reference}"
    status, challenge = manifest_status(url)
    if status == 401 and challenge.lower().startswith("bearer "):
        token = anonymous_token(challenge, repository)
        if token is None:
            return None
        if not token:
            return False
        status, _ = manifest_status(url, token)
    if status is None:
        _unreachable.add(host)
    return True if status == 200 else False if status in (401, 403, 404) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Registry lookups follow no redirects, so a registry's answer cannot send them (or a token) elsewhere."""

    def redirect_request(self, *args, **kwargs):
        return None


def registry_open(request, timeout: float):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def manifest_status(url: str, token: str | None = None) -> tuple[int | None, str]:
    """HTTP status of a manifest HEAD, with its WWW-Authenticate challenge; None when unreachable."""
    headers = {"Accept": MANIFEST_TYPES, **({"Authorization": f"Bearer {token}"} if token else {})}
    try:
        with registry_open(urllib.request.Request(url, headers=headers, method="HEAD"), REGISTRY_TIMEOUT) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers.get("WWW-Authenticate", "") or ""
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None, ""


def anonymous_token(challenge: str, repository: str) -> str | None:
    """A pull token from the challenge's realm without credentials: '' when refused, None when unreachable."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    if not realm.startswith("https://"):
        return None
    params.setdefault("scope", f"repository:{repository}:pull")
    try:
        with registry_open(f"{realm}?{urllib.parse.urlencode(params)}", REGISTRY_TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return "" if exc.code in (401, 403) else None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None
    return (data.get("token") or data.get("access_token") or "") if isinstance(data, dict) else ""


def check_cloud_build(task: Path, config: dict, evals_dir: Path, report: Report) -> None:
    """Fixed rule 8: a cloud builder, with no build arguments and no registry credentials, builds this task."""
    harness = harness_tag(evals_dir)
    for rel in ("environment/Dockerfile", "tests/Dockerfile"):
        if (task / rel).is_file():
            check_dockerfile((task / rel).read_text(errors="ignore"), rel, harness, report)
    if (task / "environment" / "docker-compose.yaml").is_file():
        report.note("environment/docker-compose.yaml: the images it names were not checked for cloud builds")
    verifier = config.get("verifier", {}) or {}
    for table, section in (("environment", config.get("environment")), ("verifier.environment", verifier.get("environment"))):
        image = section.get("docker_image") if isinstance(section, dict) else None
        if not image or not isinstance(image, str):
            continue
        if image == harness:
            report.error(f"[{table}].docker_image is the harness image; Harbor pulls docker_image from a registry "
                         "and never builds it, so start FROM the harness image in a Dockerfile instead")
        elif pullable(image) is False:
            report.warn(f"[{table}].docker_image {image!r} cannot be pulled without credentials; a cloud run "
                        "needs a registry secret for it")


def harbor_load(tasks: list[Path]) -> dict:
    if not shutil.which("uv"):
        return {}
    command = ["uv", "tool", "run", "--python", HARBOR_PYTHON, "--from", f"harbor=={HARBOR_VERSION}",
               "python", "-c", HARBOR_LOAD, *map(str, tasks)]
    try:
        ran = subprocess.run(command, capture_output=True, text=True, timeout=600)
        return json.loads(ran.stdout.strip().splitlines()[-1]) if ran.returncode == 0 else {
            str(t): f"could not run Harbor's loader: {ran.stderr.strip()[-500:]}" for t in tasks}
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return {}


def synthetic(empty: bool) -> dict:
    return {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "agent-evals-synthetic", "version": "0"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "Hello, I need help with my request."},
            {"step_id": 2, "source": "agent",
             "message": "" if empty else "Sure, I can help. Could you tell me more about what you need?"},
        ],
    }


def docker(*args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def run_verifier(task: Path, config: dict, report: Report, fixture: Path | None) -> None:
    if not shutil.which("docker"):
        report.error("--verifier needs Docker on PATH")
        return
    separate = is_separate(config)
    section = (config.get("verifier", {}) or {}).get("environment", {}) if separate else config.get("environment", {})
    image = (section or {}).get("docker_image")
    context = task / ("tests" if separate else "environment")
    if not image:
        if not (context / "Dockerfile").is_file():
            report.note(f"--verifier skipped: no Dockerfile in {context.name}/ to build the verifier from")
            return
        image = "agent-evals-check:" + hashlib.sha256(str(context.resolve()).encode()).hexdigest()[:12]
        built = docker("build", "-q", "-t", image, str(context))
        if built.returncode:
            report.error(f"verifier image build failed: {built.stderr.strip()[-1500:]}")
            return
    evaluators = declared_evaluators(config)
    codes = [ev for ev in evaluators if ev.get("type") == "code"]
    if not codes:
        report.note("no code evaluators; judges are scored after the trial, so --verifier only checks that test.sh runs")
    empty_scores: list[float] = []
    for case in ("normal", "empty-reply"):
        with tempfile.TemporaryDirectory(prefix="agent-evals-verifier-") as tmp:
            logs = Path(tmp) / "logs"
            (logs / "verifier").mkdir(parents=True)
            if fixture:
                shutil.copytree(fixture, logs / "agent")
            else:
                (logs / "agent").mkdir()
            (logs / "agent" / "trajectory.json").write_text(json.dumps(synthetic(case == "empty-reply")))
            for directory in (logs, logs / "agent", logs / "verifier"):
                directory.chmod(0o777)
            mounts = ["-v", f"{logs}:/logs"]
            if not separate:  # shared mode: Harbor uploads tests/; mount a writable copy
                tests_copy = Path(tmp) / "tests"
                shutil.copytree(task / "tests", tests_copy)
                mounts += ["-v", f"{tests_copy}:/tests"]
            # Like Harbor: make test.sh executable, run it, capture its output.
            script = ("chmod +x /tests/test.sh 2>/dev/null; "
                      "if [ -x /tests/test.sh ]; then /tests/test.sh; else sh /tests/test.sh; fi "
                      "> /logs/verifier/test-stdout.txt 2>&1; echo $? > /logs/verifier/exit-code.txt")
            try:
                ran = docker("run", "--rm", *mounts, "--entrypoint", "sh", image, "-c", script, timeout=900)
            except subprocess.TimeoutExpired:
                report.error(f"{case}: verifier did not finish within 15 minutes")
                continue
            if ran.returncode:
                report.error(f"{case}: docker run failed: {ran.stderr.strip()[-800:]}")
                continue
            rewards, feedback, errors = read_verifier_outputs(logs / "verifier", case, report)
            for ev in codes:
                ev_id, low, high = ev.get("id"), ev.get("min_score", 0.0), ev.get("max_score", 1.0)
                value = rewards.get(ev_id)
                if ev_id in errors and value is None:
                    hint = "" if fixture else (" If it reads state the harness exports to /logs/agent, pass that state "
                                               "with --fixture DIR")
                    report.error(f"{case}: evaluator {ev_id} reported an error on a well-formed trajectory: "
                                 f"{str(errors[ev_id])[-600:]}.{hint}")
                    continue
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    report.error(f"{case}: reward.json has no numeric score for declared evaluator {ev_id}")
                    continue
                if not low <= value <= high:
                    report.error(f"{case}: evaluator {ev_id} scored {value}, outside [{low}, {high}]")
                if not str(feedback.get(ev_id, "")).strip():
                    report.error(f"{case}: feedback.json has no feedback for evaluator {ev_id}")
                if case == "empty-reply":
                    empty_scores.append(normalized(value, low, high))
            for key in rewards:
                if key not in {ev.get("id") for ev in evaluators}:
                    report.warn(f"{case}: reward.json key {key!r} is not a declared evaluator")
    has_judges = any(ev.get("type") == "judge" for ev in evaluators)
    prohibition = config.get("metadata", {}).get("evals", {}).get("prohibition", False)
    if codes and not has_judges and empty_scores and min(empty_scores) >= 1.0 and not prohibition:
        report.error("every evaluator gives full marks to an empty reply, so this test cannot fail. If it only "
                     "tests a prohibition, set [metadata.evals].prohibition = true")


def read_verifier_outputs(verifier: Path, case: str, report: Report):
    rewards: dict = {}
    if (verifier / "reward.json").is_file():
        try:
            rewards = json.loads((verifier / "reward.json").read_text())
        except json.JSONDecodeError as exc:
            report.error(f"{case}: reward.json is not JSON: {exc}")
        if not isinstance(rewards, dict):
            report.error(f"{case}: reward.json must be an object of evaluator id to number")
            rewards = {}
    elif (verifier / "reward.txt").is_file():
        report.error(f"{case}: verifier wrote reward.txt; write reward.json with one key per declared code evaluator")
    else:
        stdout = (verifier / "test-stdout.txt")
        tail = stdout.read_text(errors="replace")[-800:] if stdout.is_file() else ""
        report.error(f"{case}: verifier wrote no reward file. test.sh output tail:\n{tail}")
    feedback, errors = {}, {}
    for name, target in (("feedback.json", feedback), ("errors.json", errors)):
        path = verifier / name
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    target.update(data)
                else:
                    report.error(f"{case}: {name} must be a JSON object keyed by evaluator id")
            except json.JSONDecodeError as exc:
                report.error(f"{case}: {name} is not JSON: {exc}")
    return rewards, feedback, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tasks", nargs="+", help="task directories (for example evals/tests/<id>)")
    parser.add_argument("--verifier", action="store_true", help="also run the verifier on synthetic trajectories (Docker)")
    parser.add_argument("--fixture", help="directory whose files are copied into /logs/agent for --verifier")
    parser.add_argument("--no-harbor", action="store_true", help="skip Harbor's own task loading")
    parser.add_argument("--evals-dir", help="the evals/ directory (default: inferred)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    evals_dir = evals_root(args.evals_dir)
    tasks = [Path(raw).resolve() for raw in args.tasks]
    loaded = {} if args.no_harbor else harbor_load(tasks)
    fixture = Path(args.fixture).resolve() if args.fixture else None
    status = 0
    suite = Report()
    check_harbor(evals_dir, suite, judged=has_judges(tasks))
    status |= suite.emit("evals.toml [harbor]", args.json)
    for task in tasks:
        report = Report()
        config = check_static(task, evals_dir, report)
        if loaded.get(str(task)):
            report.error(f"Harbor cannot load it: {loaded[str(task)]}")
        elif not args.no_harbor and not loaded:
            report.note("Harbor's own task loading did not run (uv missing or Harbor unavailable)")
        if args.verifier and config and not report.errors:
            run_verifier(task, config, report, fixture)
        elif args.verifier and report.errors:
            report.note("--verifier skipped until the static errors are fixed")
        status |= report.emit(f"task {test_key(task, evals_dir)}", args.json)
    return status


if __name__ == "__main__":
    sys.exit(main())
