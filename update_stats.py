#!/usr/bin/env python3
"""
Build and refresh the OpenAI GitHub repository catalog.

The script fetches public GitHub metadata for OpenAI repositories that have at
least one commit in the last three months and more than 200 stars, groups them
into purpose clusters, and rewrites index.html.

Requirements:
  - Python 3.10+
  - A GitHub token from GITHUB_TOKEN, GH_TOKEN, or an authenticated gh CLI

Usage:
  python3 update_stats.py
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ORG = "openai"
MIN_STARS = 201
TOP_PER_CLUSTER = 25
TRACTION_DAYS = 30
HISTORY_DAYS = 140
GITHUB_API = "https://api.github.com"
USER_AGENT = "openai-repos-catalog"
AUTO_KEYWORD_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OpenAI API", ("openai api", "api")),
    ("SDK", ("sdk", "client library")),
    ("OpenAPI", ("openapi", "open api")),
    ("Codex", ("codex",)),
    ("Agents", ("agents", "agent")),
    ("Apps SDK", ("apps sdk", "apps-sdk")),
    ("ChatKit", ("chatkit", "chat kit")),
    ("Realtime", ("realtime", "real-time")),
    ("Voice", ("voice", "speech", "audio")),
    ("Evals", ("evals", "evaluation")),
    ("Benchmark", ("benchmark", "bench")),
    ("Model", ("model", "models")),
    ("Safety", ("safety", "guardrails", "privacy")),
    ("CUA", ("cua", "computer using agent")),
    ("Cookbook", ("cookbook",)),
    ("Python", ("python",)),
    ("TypeScript", ("typescript", "ts")),
    ("JavaScript", ("javascript", "js", "node")),
    ("Go", ("go", "golang")),
    (".NET", (".net", "dotnet", "asp.net", "aspnet")),
    ("C#", ("c#", "csharp")),
    ("Java", ("java",)),
    ("Ruby", ("ruby",)),
    ("Rust", ("rust",)),
    ("Docker", ("docker",)),
    ("LLM", ("llm",)),
)
SUBSTRING_KEYWORDS = {
    "OpenAI API",
    "OpenAPI",
    "Apps SDK",
    "ChatKit",
    "Realtime",
    "Python",
    "TypeScript",
    "JavaScript",
    "Go",
    ".NET",
    "C#",
    "Java",
    "Ruby",
    "Rust",
}


@dataclass(frozen=True)
class Cluster:
    key: str
    name: str
    summary: str
    accent: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class SourceSection:
    key: str
    name: str
    summary: str
    query: str
    accent: str


CLUSTERS: tuple[Cluster, ...] = (
    Cluster(
        "sdk-api-clients",
        "API SDKs, Specs & CLIs",
        "Official client libraries, generated API surfaces, OpenAPI specs, and command-line API tooling.",
        "green",
        (
            ".net",
            "api",
            "c#",
            "client",
            "client library",
            "cli",
            "dotnet",
            "go",
            "java",
            "library",
            "node",
            "openapi",
            "python",
            "ruby",
            "sdk",
            "typescript",
        ),
    ),
    Cluster(
        "agents-codex",
        "Agents, Codex & Automation",
        "Agent frameworks, Codex tooling, delegated coding workflows, CUA samples, and autonomous work orchestration.",
        "purple",
        (
            "agent",
            "agents",
            "automation",
            "autonomous",
            "codex",
            "computer using agent",
            "cua",
            "delegate",
            "multi-agent",
            "orchestration",
            "skills",
            "swarm",
            "symphony",
            "terminal",
            "workflow",
        ),
    ),
    Cluster(
        "apps-chatkit",
        "Apps SDK, ChatKit & Product UI",
        "Apps SDK examples, ChatKit packages, UI starters, plugins, and product-facing integration surfaces.",
        "blue",
        (
            "app",
            "apps",
            "apps sdk",
            "apps-sdk",
            "chatkit",
            "component",
            "plugin",
            "plugins",
            "starter",
            "ui",
        ),
    ),
    Cluster(
        "cookbooks-examples",
        "Cookbooks, Examples & Demos",
        "Examples, starter applications, notebooks, workshops, demo apps, and implementation playbooks.",
        "yellow",
        (
            "cookbook",
            "demo",
            "example",
            "examples",
            "guide",
            "notebook",
            "quickstart",
            "sample",
            "starter",
            "workshop",
        ),
    ),
    Cluster(
        "realtime-voice-multimodal",
        "Realtime, Voice & Multimodal",
        "Realtime API demos, speech and audio systems, multimodal interaction, and voice interface tooling.",
        "cyan",
        (
            "audio",
            "clip",
            "fm",
            "image",
            "meeting assistant",
            "multimodal",
            "realtime",
            "real-time",
            "solar system",
            "speech",
            "voice",
            "whisper",
        ),
    ),
    Cluster(
        "research-models",
        "Models, Research & Tooling",
        "Model releases, research code, tokenizers, interpretability tools, response formats, and training experiments.",
        "orange",
        (
            "clip",
            "debugger",
            "gpt-oss",
            "harmony",
            "interpretability",
            "model",
            "parameter",
            "procgen",
            "research",
            "tiktoken",
            "token",
            "tokenizer",
            "training",
            "transformer",
            "whisper",
        ),
    ),
    Cluster(
        "evals-benchmarks",
        "Evals, Benchmarks & Measurement",
        "Evaluation frameworks, benchmark suites, measurement tools, and model quality or capability testbeds.",
        "pink",
        (
            "bench",
            "benchmark",
            "eval",
            "evals",
            "evaluation",
            "frontier",
            "gabriel",
            "mle-bench",
            "measurement",
            "metrics",
            "social scientists",
            "testbed",
        ),
    ),
    Cluster(
        "safety-governance",
        "Safety, Privacy & Governance",
        "Guardrails, privacy filters, model policy, safety tooling, and security automation.",
        "red",
        (
            "bot",
            "bots",
            "filter",
            "governance",
            "guardrail",
            "guardrails",
            "model spec",
            "policy",
            "privacy",
            "safety",
            "security",
            "spec",
        ),
    ),
)


EXTRA_SECTIONS: tuple[SourceSection, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update OpenAI GitHub repository catalog.")
    parser.add_argument("--file", default="index.html", help="HTML file to write")
    parser.add_argument("--history", default="stats_history.json", help="history JSON file")
    parser.add_argument("--org", default=ORG, help="GitHub organization")
    parser.add_argument("--min-stars", type=int, default=MIN_STARS, help="minimum stars")
    parser.add_argument("--top-per-cluster", type=int, default=TOP_PER_CLUSTER)
    parser.add_argument("--traction-days", type=int, default=TRACTION_DAYS)
    parser.add_argument("--months", type=int, default=3, help="recency window in calendar months")
    parser.add_argument("--skip-commit-counts", action="store_true", help="skip per-repo commit counts")
    parser.add_argument("--skip-content", action="store_true", help="skip README and repository content enrichment")
    return parser.parse_args()


def subtract_months(dt: datetime, months: int) -> datetime:
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, days_in_month[month - 1]))


def get_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        token = result.stdout.strip()
        return token or None
    return None


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def request(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, headers=headers)
        for attempt in range(4):
            try:
                with urlopen(req, timeout=60) as response:
                    body = response.read().decode("utf-8")
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    return json.loads(body), response_headers
            except HTTPError as exc:
                if exc.code in (403, 429) and attempt < 3:
                    reset = exc.headers.get("X-RateLimit-Reset")
                    if reset and reset.isdigit():
                        delay = max(2, min(60, int(reset) - int(time.time()) + 2))
                    else:
                        delay = 3 * (attempt + 1)
                    time.sleep(delay)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"GitHub API failed for {url}: HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"GitHub API failed for {url}: {exc}") from exc
        raise RuntimeError(f"GitHub API failed for {url}")


def parse_link_header(link: str | None) -> dict[str, str]:
    links: dict[str, str] = {}
    if not link:
        return links
    for part in link.split(","):
        m = re.search(r'<([^>]+)>;\s*rel="([^"]+)"', part.strip())
        if m:
            links[m.group(2)] = m.group(1)
    return links


def link_last_page(link: str | None, fallback_len: int) -> int:
    links = parse_link_header(link)
    last = links.get("last")
    if not last:
        return fallback_len
    m = re.search(r"[?&]page=(\d+)", last)
    return int(m.group(1)) if m else fallback_len


def iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_number(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    value = int(value)
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
        return text.replace(".0M", "M")
    if value >= 100_000:
        return f"{value / 1000:.0f}k"
    if value >= 1_000:
        text = f"{value / 1000:.1f}k"
        return text.replace(".0k", "k")
    return str(value)


def fmt_date(value: str) -> str:
    if not value:
        return "n/a"
    return iso_to_datetime(value).strftime("%b %d, %Y")


def read_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"snapshots": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"snapshots": []}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"snapshots": []}
    return data


def write_history(path: Path, history: dict[str, Any], repos: list[dict[str, Any]], now: datetime) -> None:
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    snapshots = []
    for snap in history.get("snapshots", []):
        try:
            snap_date = datetime.strptime(snap["date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        if snap_date >= cutoff:
            snapshots.append(snap)

    today = now.strftime("%Y-%m-%d")
    snapshots = [snap for snap in snapshots if snap.get("date") != today]
    snapshots.append(
        {
            "date": today,
            "repos": {
                repo["full_name"]: {
                    "stars": repo["stars"],
                    "forks": repo["forks"],
                    "pushed_at": repo["pushed_at"],
                }
                for repo in repos
            },
        }
    )
    snapshots.sort(key=lambda snap: snap["date"])
    path.write_text(json.dumps({"snapshots": snapshots}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def baseline_snapshot(history: dict[str, Any], now: datetime, days: int) -> dict[str, Any] | None:
    target = now.date() - timedelta(days=days)
    candidates = []
    for snap in history.get("snapshots", []):
        try:
            snap_date = datetime.strptime(snap["date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        if snap_date <= target:
            candidates.append((snap_date, snap))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def fetch_repositories(client: GitHubClient, org: str, min_stars: int, pushed_cutoff: datetime) -> list[dict[str, Any]]:
    query = f"org:{org} stars:>={min_stars} pushed:>={pushed_cutoff.date().isoformat()} fork:false archived:false"
    repos = fetch_search_repositories(client, query, pushed_cutoff)
    repos = [repo for repo in repos if repo["stars"] >= min_stars]
    repos.sort(key=lambda repo: repo["stars"], reverse=True)
    return repos


def fetch_extra_section_repositories(client: GitHubClient, section: SourceSection, pushed_cutoff: datetime) -> list[dict[str, Any]]:
    query = f"{section.query} pushed:>={pushed_cutoff.date().isoformat()}"
    repos = fetch_search_repositories(client, query, pushed_cutoff)
    for repo in repos:
        repo["cluster_key"] = section.key
        repo["cluster_name"] = section.name
    repos.sort(key=lambda repo: (iso_to_datetime(repo["pushed_at"]), repo["stars"]), reverse=True)
    return repos


def fetch_search_repositories(client: GitHubClient, query: str, pushed_cutoff: datetime) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        data, _ = client.request(
            "/search/repositories",
            {
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            if iso_to_datetime(item["pushed_at"]) < pushed_cutoff:
                continue
            repos.append(normalize_repo(item))
        if len(items) < 100 or page >= 10:
            break
        page += 1
    return repos


def normalize_repo(item: dict[str, Any]) -> dict[str, Any]:
    license_info = item.get("license") or {}
    license_spdx = license_info.get("spdx_id") if isinstance(license_info, dict) else None
    if not license_spdx or license_spdx == "NOASSERTION":
        license_spdx = "n/a"
    return {
        "name": item.get("name", ""),
        "full_name": item.get("full_name", ""),
        "url": item.get("html_url", ""),
        "description": item.get("description") or "",
        "language": item.get("language") or "Mixed",
        "topics": item.get("topics") or [],
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "pushed_at": item.get("pushed_at") or "",
        "updated_at": item.get("updated_at") or "",
        "created_at": item.get("created_at") or "",
        "default_branch": item.get("default_branch") or "main",
        "license": license_spdx,
        "commits": None,
        "commits_30d": None,
        "content": {},
    }


def enrich_commit_counts(
    client: GitHubClient,
    repos: list[dict[str, Any]],
    traction_since: datetime,
    eligibility_since: datetime,
) -> None:
    for index, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        print(f"[{index:03d}/{len(repos):03d}] commits {full_name}")
        repo["commits"] = commit_count(client, full_name)
        repo["commits_30d"] = commit_count(client, full_name, since=traction_since)
        repo["commits_since_cutoff"] = commit_count(client, full_name, since=eligibility_since)


def commit_count(client: GitHubClient, full_name: str, since: datetime | None = None) -> int:
    params: dict[str, Any] = {"per_page": 1}
    if since:
        params["since"] = since.isoformat().replace("+00:00", "Z")
    try:
        data, headers = client.request(f"/repos/{full_name}/commits", params)
    except RuntimeError as exc:
        print(f"  warning: {exc}", file=sys.stderr)
        return 0
    if not isinstance(data, list) or not data:
        return 0
    return link_last_page(headers.get("link"), len(data))


def enrich_content_profiles(client: GitHubClient, repos: list[dict[str, Any]]) -> None:
    for index, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        print(f"[{index:03d}/{len(repos):03d}] content {full_name}")
        repo["content"] = repo_content_profile(client, repo)


def repo_content_profile(client: GitHubClient, repo: dict[str, Any]) -> dict[str, Any]:
    readme_text = fetch_readme_text(client, repo["full_name"])
    headings = extract_markdown_headings(readme_text)
    tree_paths = fetch_tree_paths(client, repo["full_name"], repo.get("default_branch") or "main")
    return build_content_profile(readme_text, headings, tree_paths)


def fetch_readme_text(client: GitHubClient, full_name: str) -> str:
    try:
        data, _ = client.request(f"/repos/{full_name}/readme")
    except RuntimeError as exc:
        print(f"  warning: README unavailable for {full_name}: {exc}", file=sys.stderr)
        return ""
    content = data.get("content")
    if not isinstance(content, str):
        return ""
    try:
        return base64.b64decode(content, validate=False).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def fetch_tree_paths(client: GitHubClient, full_name: str, branch: str) -> list[str]:
    safe_branch = quote(branch, safe="")
    try:
        data, _ = client.request(f"/repos/{full_name}/git/trees/{safe_branch}", {"recursive": "1"})
    except RuntimeError as exc:
        print(f"  warning: tree unavailable for {full_name}: {exc}", file=sys.stderr)
        return []
    tree = data.get("tree")
    if not isinstance(tree, list):
        return []
    paths = []
    for item in tree:
        path = item.get("path") if isinstance(item, dict) else None
        if isinstance(path, str) and path:
            paths.append(path)
    return paths


def extract_markdown_headings(text: str, limit: int = 6) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", stripped)
        if not match:
            continue
        level = len(match.group(1))
        label = clean_heading(match.group(2))
        if not label:
            continue
        if level == 1 and not headings:
            continue
        if label.lower() in {item.lower() for item in headings}:
            continue
        headings.append(label)
        if len(headings) >= limit:
            break
    return headings


def clean_heading(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\]\([^)]+\)", "]", value)
    value = re.sub(r"https?://\S+", "", value)
    value = value.replace("[", "").replace("]", "")
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" #:-")
    return value[:72]


def build_content_profile(readme_text: str, headings: list[str], paths: list[str]) -> dict[str, Any]:
    lower_paths = [path.lower() for path in paths]
    root_dirs = sorted({path.split("/", 1)[0] for path in paths if "/" in path})
    root_files = sorted({path for path in paths if "/" not in path})
    important_paths = select_important_paths(paths, root_dirs, root_files)
    badges = content_badges(lower_paths, root_dirs, root_files, headings)
    return {
        "has_readme": bool(readme_text.strip()),
        "readme_sections": headings[:6],
        "important_paths": important_paths[:7],
        "badges": badges[:8],
        "tree_count": len(paths),
    }


def select_important_paths(paths: list[str], root_dirs: list[str], root_files: list[str]) -> list[str]:
    lower_to_path = {path.lower(): path for path in paths}
    candidates: list[str] = []
    priority = (
        "README.md",
        "docs/",
        "examples/",
        "example/",
        "samples/",
        "sample/",
        "notebooks/",
        "src/",
        "packages/",
        "apps/",
        "app/",
        "python/",
        "javascript/",
        "typescript/",
        "openapi.yaml",
        "openapi.json",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "Dockerfile",
    )
    roots = {f"{item}/" for item in root_dirs}
    files = set(root_files)
    for item in priority:
        lower = item.lower()
        if item.endswith("/") and lower[:-1] in {root.lower() for root in root_dirs}:
            candidates.append(item)
        elif item in files:
            candidates.append(item)
        elif lower in lower_to_path:
            candidates.append(lower_to_path[lower])

    for root in ("docs", "examples", "samples", "notebooks", "src", "packages", "apps", "tests"):
        if f"{root}/" in roots and f"{root}/" not in candidates:
            candidates.append(f"{root}/")

    seen: set[str] = set()
    result: list[str] = []
    for path in candidates:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def content_badges(lower_paths: list[str], root_dirs: list[str], root_files: list[str], headings: list[str]) -> list[str]:
    roots = {root.lower() for root in root_dirs}
    files = {file.lower() for file in root_files}
    all_text = " ".join([*lower_paths, *[heading.lower() for heading in headings]])
    checks: tuple[tuple[str, bool], ...] = (
        ("README", bool(headings)),
        ("Docs", "docs" in roots or "documentation" in all_text),
        ("Examples", any(root in roots for root in ("examples", "example", "samples", "sample")) or "example" in all_text),
        ("Tests", any(root in roots for root in ("test", "tests", "testing")) or "/test" in all_text),
        ("Packages", any(file in files for file in ("package.json", "pyproject.toml", "setup.py", "go.mod", "cargo.toml", "pom.xml", "build.gradle", "gemfile", "mix.exs"))),
        ("Notebooks", any(path.endswith(".ipynb") for path in lower_paths)),
        ("Docker", "dockerfile" in files or "docker" in roots),
        ("OpenAPI", any("openapi" in path for path in lower_paths)),
        ("Workflows", any(path.startswith(".github/workflows/") for path in lower_paths)),
    )
    return [label for label, present in checks if present]


def cluster_repo(repo: dict[str, Any]) -> Cluster:
    haystack = " ".join(
        [
            repo["name"],
            repo["description"],
            repo["language"],
            " ".join(repo.get("topics") or []),
        ]
    ).lower()
    override_terms = {
        "safety-governance": (
            "guardrail",
            "guardrails",
            "model spec",
            "openai-guardrails-python",
            "policy",
            "privacy",
            "safety",
            "security",
            "spec",
        ),
        "sdk-api-clients": (
            "openai-cli",
            "openai-dotnet",
            "openai-go",
            "openai-java",
            "openai-node",
            "openai-openapi",
            "openai-python",
            "openai-ruby",
        ),
        "agents-codex": (
            "codex",
            "computer using agent",
            "cua",
            "multi-agent",
            "openai-agents",
            "skills",
            "swarm",
            "symphony",
        ),
        "apps-chatkit": (
            "apps-sdk",
            "chatkit",
            "plugin",
            "plugins",
        ),
        "evals-benchmarks": (
            "bench",
            "eval",
            "evals",
            "gabriel",
            "mle-bench",
            "parameter-golf",
        ),
        "realtime-voice-multimodal": (
            "audio",
            "fm",
            "meeting assistant",
            "multimodal",
            "realtime",
            "real-time",
            "solar system",
            "speech",
            "voice",
            "whisper",
        ),
        "research-models": (
            "clip",
            "gpt-oss",
            "harmony",
            "procgen",
            "tiktoken",
            "transformer-debugger",
        ),
    }
    clusters_by_key = {cluster.key: cluster for cluster in CLUSTERS}
    for key, terms in override_terms.items():
        if any(term_matches(haystack, term) for term in terms):
            return clusters_by_key[key]

    scores: list[tuple[int, int, Cluster]] = []
    for order, cluster in enumerate(CLUSTERS):
        score = 0
        for keyword in cluster.keywords:
            if keyword in haystack:
                score += 2 if keyword in repo["name"].lower() else 1
        scores.append((score, -order, cluster))
    best = max(scores, key=lambda item: (item[0], item[1]))
    if best[0] == 0:
        return CLUSTERS[0]
    return best[2]


def term_matches(haystack: str, term: str) -> bool:
    if " " in term:
        return term in haystack
    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def add_derived_fields(repos: list[dict[str, Any]], history: dict[str, Any], now: datetime, traction_days: int) -> None:
    baseline = baseline_snapshot(history, now, traction_days)
    baseline_repos = baseline.get("repos", {}) if baseline else {}
    for repo in repos:
        cluster = cluster_repo(repo)
        repo["cluster_key"] = cluster.key
        repo["cluster_name"] = cluster.name
        old = baseline_repos.get(repo["full_name"])
        if old:
            repo["stars_delta_30d"] = max(0, repo["stars"] - int(old.get("stars", repo["stars"])))
            repo["forks_delta_30d"] = max(0, repo["forks"] - int(old.get("forks", repo["forks"])))
            repo["has_history_baseline"] = True
        else:
            repo["stars_delta_30d"] = None
            repo["forks_delta_30d"] = None
            repo["has_history_baseline"] = False
        repo["traction_score"] = traction_score(repo)


def traction_score(repo: dict[str, Any]) -> float:
    commits = int(repo.get("commits_30d") or 0)
    audience = (math.log10(max(repo["stars"], 10)) * 18) + (math.log10(max(repo["forks"], 1) + 1) * 6)
    if repo.get("has_history_baseline"):
        star_delta = int(repo.get("stars_delta_30d") or 0)
        fork_delta = int(repo.get("forks_delta_30d") or 0)
        return (star_delta * 10) + (fork_delta * 4) + min(commits, 350) + audience

    last_push = iso_to_datetime(repo["pushed_at"])
    age_days = max(0, (datetime.now(timezone.utc) - last_push).days)
    freshness = max(0.0, 1.0 - (age_days / TRACTION_DAYS))
    return min(commits, 350) + (freshness * audience)


def grouped_repos(repos: list[dict[str, Any]], top_n: int) -> dict[str, list[dict[str, Any]]]:
    groups = {cluster.key: [] for cluster in CLUSTERS}
    for repo in repos:
        groups[repo["cluster_key"]].append(repo)
    for key in groups:
        groups[key].sort(key=lambda repo: repo["stars"], reverse=True)
        groups[key] = groups[key][:top_n]
    return groups


def language_class(language: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", language.lower()).strip("-")
    known = {
        "c": "c",
        "c#": "csharp",
        "c++": "cpp",
        "cuda": "cuda",
        "go": "go",
        "javascript": "js",
        "python": "py",
        "shell": "shell",
        "typescript": "ts",
    }
    return known.get(language.lower(), normalized or "generic")


def detected_keywords(repo: dict[str, Any]) -> list[str]:
    haystack = f"{repo['name']} {repo['description']}".lower()
    found = []
    for label, aliases in AUTO_KEYWORD_TERMS:
        if label in SUBSTRING_KEYWORDS:
            matched = any(alias in haystack for alias in aliases)
        else:
            matched = any(term_matches(haystack, alias) for alias in aliases)
        if matched:
            found.append(label)
    return found


def keyword_buttons(repo: dict[str, Any]) -> str:
    keywords: list[str] = []
    seen: set[str] = set()
    for keyword in [*detected_keywords(repo), *(repo.get("topics") or [])]:
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)

    return "".join(
        (
            f'<button class="badge keyword-button" type="button" '
            f'data-filter-keyword="{escape(keyword)}" title="Filter by {escape(keyword)}">'
            f'{escape(keyword)}</button>'
        )
        for keyword in keywords
    )


def repo_row(repo: dict[str, Any], rank: int | None = None, include_cluster: bool = False, include_traction: bool = False) -> str:
    pushed_ts = int(iso_to_datetime(repo["pushed_at"]).timestamp()) if repo.get("pushed_at") else 0
    desc = escape(repo["description"] or "No description provided.")
    topic_html = keyword_buttons(repo)
    owner = repo["full_name"].split("/", 1)[0]
    avatar = f"https://github.com/{escape(owner)}.png?size=32"
    name_cell = (
        f'<div class="repo-name"><img src="{avatar}" alt="" class="avatar">'
        f'<a href="{escape(repo["url"])}" target="_blank" rel="noreferrer">{escape(repo["name"])}</a></div>'
        f'<a class="repo-slug" href="{escape(repo["url"])}" target="_blank" rel="noreferrer">{escape(repo["full_name"])}</a>'
    )
    language = escape(repo["language"])
    cells = []
    if rank is not None:
        cells.append(f'<td class="rank-cell">{rank}</td>')
        cells.append(f"<td>{name_cell}</td>")
        cells.append(description_cell(repo, desc, topic_html))
        if include_cluster:
            cells.append(cluster_cell(repo, linked=include_traction))
        cells.append(f'<td><span class="tag tag-{language_class(repo["language"])}">{language}</span></td>')
    else:
        cells.append(f"<td>{name_cell}</td>")
        cells.append(f'<td><span class="tag tag-{language_class(repo["language"])}">{language}</span></td>')
        cells.append(description_cell(repo, desc, topic_html))
        if include_cluster:
            cells.append(cluster_cell(repo, linked=include_traction))
    cells.append(github_cell(repo, include_activity=include_traction))
    return (
        f'<tr data-stars="{repo["stars"]}" data-pushed="{pushed_ts}" '
        f'data-name="{escape(repo["name"].lower())}" data-cluster="{escape(repo["cluster_key"])}">\n'
        + "\n".join(f"  {cell}" for cell in cells)
        + "\n</tr>"
    )


def description_cell(repo: dict[str, Any], desc: str, topic_html: str) -> str:
    return f'<td class="description-cell">{desc}<div class="topic-row">{topic_html}</div>{content_profile_html(repo)}</td>'


def content_profile_html(repo: dict[str, Any]) -> str:
    content = repo.get("content") or {}
    if not isinstance(content, dict):
        return ""

    sections = [escape(item) for item in content.get("readme_sections", []) if item]
    paths = [str(item) for item in content.get("important_paths", []) if item]
    badges = [escape(item) for item in content.get("badges", []) if item]
    blocks = []
    if sections:
        blocks.append(
            '<div class="content-line"><span class="content-label">README</span>'
            f'<span class="content-text">{" · ".join(sections[:5])}</span></div>'
        )
    if paths:
        path_links = " ".join(content_path_link(repo, path) for path in paths[:6])
        blocks.append(
            '<div class="content-line"><span class="content-label">Paths</span>'
            f'<span class="content-text content-paths">{path_links}</span></div>'
        )
    if badges:
        badge_html = "".join(f'<span class="content-badge">{badge}</span>' for badge in badges[:8])
        blocks.append(f'<div class="content-badge-row">{badge_html}</div>')
    if not blocks:
        return ""
    return f'<div class="content-profile">{"".join(blocks)}</div>'


def content_path_link(repo: dict[str, Any], path: str) -> str:
    branch = quote(str(repo.get("default_branch") or "main"), safe="")
    clean_path = path.rstrip("/")
    encoded_path = quote(clean_path, safe="/")
    kind = "tree" if path.endswith("/") else "blob"
    url = f'{repo["url"]}/{kind}/{branch}/{encoded_path}'
    return f'<a class="content-path" href="{escape(url)}" target="_blank" rel="noreferrer">{escape(path)}</a>'


def cluster_cell(repo: dict[str, Any], linked: bool = False) -> str:
    name = escape(repo["cluster_name"])
    if linked:
        return f'<td><a class="cluster-pill cluster-link" href="#cluster-{escape(repo["cluster_key"])}">{name}</a></td>'
    return f'<td><span class="cluster-pill">{name}</span></td>'


def github_cell(repo: dict[str, Any], include_activity: bool = False) -> str:
    activity = ""
    if include_activity:
        pct = max(4, min(100, int(repo.get("traction_pct", 0))))
        activity = f"""
    <div class="activity-block" aria-label="Recent activity score {int(repo["traction_score"])}">
      <div class="activity-bar"><span style="width: {pct}%"></span></div>
      <div class="activity-meta">
        <span>{fmt_number(repo.get("commits_30d"))} commits / 30d</span>
        <span>score {int(repo["traction_score"])}</span>
      </div>
    </div>"""
    return f"""<td class="github-cell">
    <div class="gh-stats">
      <span class="star-count">★ {fmt_number(repo["stars"])}</span>
      <span class="fork-count">⑂ {fmt_number(repo["forks"])}</span>
      <span class="commit-count">⟳ {fmt_number(repo.get("commits"))}</span>
    </div>
    <span class="last-updated">⏱ {fmt_date(repo["pushed_at"])}</span>
    {activity}
  </td>"""


def section_table(cluster: Cluster, repos: list[dict[str, Any]], top_n: int) -> str:
    rows = "\n".join(repo_row(repo) for repo in repos)
    return f"""
<section class="repo-section" id="cluster-{cluster.key}" data-section>
  <div class="section-header">
    <div>
      <h2><span class="section-mark section-mark-{cluster.accent}"></span>{escape(cluster.name)}</h2>
      <p>{escape(cluster.summary)} Showing up to {top_n} repositories by stars.</p>
    </div>
    <span class="count-pill">{len(repos)} repos</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="col-repository">
        <col class="col-language">
        <col class="col-description">
        <col class="col-github">
      </colgroup>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Language</th>
          <th>Description & Content</th>
          <th>GitHub</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>
"""


def source_section_table(section: SourceSection, repos: list[dict[str, Any]]) -> str:
    rows = "\n".join(repo_row(repo) for repo in repos)
    return f"""
<section class="repo-section" id="cluster-{section.key}" data-section>
  <div class="section-header">
    <div>
      <h2><span class="section-mark section-mark-{section.accent}"></span>{escape(section.name)}</h2>
      <p>{escape(section.summary)} Showing {len(repos)} repositories sorted by recent activity.</p>
    </div>
    <span class="count-pill">{len(repos)} repos</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="col-repository">
        <col class="col-language">
        <col class="col-description">
        <col class="col-github">
      </colgroup>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Language</th>
          <th>Description & Content</th>
          <th>GitHub</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>
"""


def traction_table(repos: list[dict[str, Any]], days: int) -> str:
    top = sorted(repos, key=lambda repo: repo["traction_score"], reverse=True)[:25]
    max_score = max((repo["traction_score"] for repo in top), default=1) or 1
    for repo in top:
        repo["traction_pct"] = round((repo["traction_score"] / max_score) * 100)
    rows = "\n".join(repo_row(repo, include_cluster=True, include_traction=True) for repo in top)
    return f"""
<section class="repo-section traction-section" id="section-traction" data-section>
  <div class="section-header">
    <div>
      <h2><span class="section-mark section-mark-blue"></span>Established Repos With Fresh Traction</h2>
      <p>Ranked by recent commits, freshness, stars, and forks so active projects with broad audiences rise first.</p>
    </div>
    <span class="count-pill">Top 25</span>
  </div>
  <div class="table-wrap">
    <table>
      <colgroup>
        <col class="col-traction-repository">
        <col class="col-traction-language">
        <col class="col-traction-description">
        <col class="col-traction-cluster">
        <col class="col-traction-github">
      </colgroup>
      <thead>
        <tr>
          <th>Repository</th>
          <th>Language</th>
          <th>Description & Content</th>
          <th>Cluster</th>
          <th>GitHub</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</section>
"""


def jump_links(extra_sections: dict[str, list[dict[str, Any]]]) -> str:
    cluster_items = [
        ("section-traction", "Fresh Traction", None),
        *[(f"cluster-{cluster.key}", cluster.name, None) for cluster in CLUSTERS],
    ]
    extra_items = [
        (f"cluster-{section.key}", section.name, len(extra_sections.get(section.key, [])))
        for section in EXTRA_SECTIONS
    ]

    def render_group(label: str, items: list[tuple[str, str, int | None]], class_name: str) -> str:
        links = "\n".join(
            f'<a href="#{escape(anchor)}">{escape(item_label)}{f" <span>{count}</span>" if count is not None else ""}</a>'
            for anchor, item_label, count in items
        )
        return f'<div class="jump-group {class_name}"><span class="jump-group-label">{escape(label)}</span>{links}</div>'

    groups = [render_group("Clusters", cluster_items, "jump-group-clusters")]
    if extra_items:
        groups.append(render_group("Other Repos", extra_items, "jump-group-other"))
    return "\n".join(groups)


def render_html(
    repos: list[dict[str, Any]],
    extra_sections: dict[str, list[dict[str, Any]]],
    now: datetime,
    pushed_cutoff: datetime,
    args: argparse.Namespace,
) -> str:
    groups = grouped_repos(repos, args.top_per_cluster)
    total_stars = sum(repo["stars"] for repo in repos)
    total_commits_30d = sum(int(repo.get("commits_30d") or 0) for repo in repos)
    total_content_profiles = sum(1 for repo in repos if (repo.get("content") or {}).get("has_readme"))
    sections = "\n".join(section_table(cluster, groups[cluster.key], args.top_per_cluster) for cluster in CLUSTERS)
    extra_section_html = "\n".join(
        source_section_table(section, extra_sections.get(section.key, []))
        for section in EXTRA_SECTIONS
    )
    traction = traction_table(repos, args.traction_days)
    nav_links = jump_links(extra_sections)
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    cutoff = pushed_cutoff.strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script>
  var p = location.pathname;
  if (p.charAt(p.length - 1) !== '/') p += '/';
  document.write('<base href="' + p + '">');
</script>
<title>OpenAI GitHub Repository Atlas</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #09090a;
    --surface: #111113;
    --surface2: #171719;
    --surface3: #202123;
    --border: #2b2c30;
    --text: #f7f7f2;
    --text-muted: #a7a7a0;
    --openai: #10a37f;
    --blue: #5ac8fa;
    --cyan: #20d9c7;
    --green: #10a37f;
    --orange: #f4a261;
    --red: #ff6b6b;
    --pink: #d48cff;
    --purple: #8b7cf6;
    --yellow: #f2c94c;
  }}

  * {{ box-sizing: border-box; }}

  html {{ scroll-behavior: smooth; }}

  body {{
    margin: 0;
    min-height: 100vh;
    background:
      radial-gradient(circle at 82% -10%, rgba(16, 163, 127, 0.2), transparent 28rem),
      radial-gradient(circle at 8% 4%, rgba(90, 200, 250, 0.12), transparent 26rem),
      linear-gradient(180deg, #111315 0%, var(--bg) 42rem);
    color: var(--text);
    font-family: 'DM Sans', system-ui, sans-serif;
    padding: 40px 24px 52px;
  }}

  a {{ color: inherit; }}

  .header,
  .search-panel,
  .repo-section,
  .footer {{
    max-width: 1480px;
    margin-left: auto;
    margin-right: auto;
  }}

  .page-header {{
    margin-bottom: 24px;
  }}

  .page-title {{
    display: flex;
    align-items: center;
    gap: 14px;
    margin: 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 30px;
    line-height: 1.15;
    letter-spacing: -0.6px;
  }}

  .logo-box {{
    width: 38px;
    height: 38px;
    display: inline-grid;
    place-items: center;
    border-radius: 8px;
    border: 1px solid rgba(16, 163, 127, 0.68);
    background:
      radial-gradient(circle at 50% 25%, rgba(247, 247, 242, 0.2), transparent 38%),
      linear-gradient(145deg, rgba(16, 163, 127, 0.28), rgba(17, 17, 19, 0.96));
    color: #f7f7f2;
    font-weight: 800;
    box-shadow: 0 0 24px rgba(16, 163, 127, 0.26);
  }}

  .page-header p {{
    max-width: 920px;
    margin: 10px 0 0;
    color: var(--text-muted);
    line-height: 1.6;
    font-size: 14px;
  }}

  .metric-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 18px;
  }}

  .metric-card {{
    min-width: 148px;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(17, 22, 26, 0.8);
  }}

  .metric-card strong {{
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px;
    color: var(--text);
  }}

  .metric-card span {{
    display: block;
    margin-top: 3px;
    color: var(--text-muted);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }}

  .jump-nav {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    margin-top: 18px;
    align-items: flex-start;
  }}

  .jump-group {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    align-items: center;
    padding: 8px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: rgba(17, 22, 26, 0.48);
  }}

  .jump-group-label {{
    padding: 0 4px 0 2px;
    color: var(--text-muted);
    font: 700 10px 'JetBrains Mono', monospace;
    letter-spacing: 0.7px;
    text-transform: uppercase;
  }}

  .jump-nav a {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 8px 10px;
    background: rgba(17, 22, 26, 0.72);
    color: var(--text);
    text-decoration: none;
    font: 700 11px 'JetBrains Mono', monospace;
    transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
  }}

  .jump-group-clusters a {{
    border-color: rgba(16, 163, 127, 0.38);
    background: rgba(16, 163, 127, 0.08);
  }}

  .jump-group-other {{
    border-color: rgba(32, 217, 199, 0.22);
    background: rgba(32, 217, 199, 0.045);
  }}

  .jump-group-other a {{
    border-color: rgba(32, 217, 199, 0.36);
    background: rgba(32, 217, 199, 0.08);
  }}

  .jump-nav a:hover {{
    border-color: var(--openai);
    color: var(--openai);
    background: rgba(16, 163, 127, 0.1);
  }}

  .jump-group-other a:hover {{
    border-color: var(--cyan);
    color: var(--cyan);
    background: rgba(32, 217, 199, 0.12);
  }}

  .jump-nav span {{
    color: var(--text-muted);
    font-size: 10px;
  }}

  .search-panel {{
    display: grid;
    grid-template-columns: auto minmax(240px, 1fr) auto auto auto auto;
    gap: 10px;
    align-items: center;
    margin-bottom: 30px;
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background:
      radial-gradient(circle at top right, rgba(16, 163, 127, 0.14), transparent 24rem),
      linear-gradient(180deg, rgba(23, 24, 26, 0.98), rgba(17, 17, 19, 0.98));
  }}

  .search-label,
  .sort-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-muted);
    white-space: nowrap;
  }}

  .search-input {{
    width: 100%;
    min-width: 0;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(8, 11, 13, 0.95);
    color: var(--text);
    outline: none;
    padding: 12px 14px;
    font: 500 14px 'DM Sans', system-ui, sans-serif;
  }}

  .search-input:focus {{
    border-color: rgba(16, 163, 127, 0.82);
    box-shadow: 0 0 0 3px rgba(16, 163, 127, 0.14);
  }}

  .button {{
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(8, 11, 13, 0.95);
    color: var(--text);
    padding: 12px 13px;
    font: 700 12px 'JetBrains Mono', monospace;
    cursor: pointer;
    white-space: nowrap;
  }}

  .button:hover,
  .button.active {{
    border-color: var(--openai);
    color: var(--openai);
  }}

  .button:disabled {{
    opacity: 0.55;
    cursor: default;
  }}

  .repo-section {{
    margin-top: 34px;
  }}

  .section-header {{
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: end;
    margin-bottom: 14px;
  }}

  .section-header h2 {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 20px;
    line-height: 1.25;
    letter-spacing: -0.3px;
  }}

  .section-header p {{
    margin: 6px 0 0;
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.5;
  }}

  .section-mark {{
    width: 10px;
    height: 20px;
    border-radius: 2px;
    background: var(--openai);
    display: inline-block;
  }}

  .section-mark-blue {{ background: var(--blue); }}
  .section-mark-cyan {{ background: var(--cyan); }}
  .section-mark-orange {{ background: var(--orange); }}
  .section-mark-pink {{ background: var(--pink); }}
  .section-mark-purple {{ background: var(--purple); }}
  .section-mark-green {{ background: var(--green); }}
  .section-mark-red {{ background: var(--red); }}
  .section-mark-yellow {{ background: var(--yellow); }}

  .count-pill,
  .cluster-pill {{
    display: inline-block;
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 4px 8px;
    color: var(--text-muted);
    background: rgba(8, 11, 13, 0.45);
    font: 600 11px 'JetBrains Mono', monospace;
  }}

  .count-pill {{
    white-space: nowrap;
  }}

  .cluster-pill {{
    white-space: normal;
    line-height: 1.35;
  }}

  .cluster-link {{
    text-decoration: none;
    transition: border-color 0.15s ease, color 0.15s ease;
  }}

  .cluster-link:hover {{
    color: var(--openai);
    border-color: var(--openai);
  }}

  .table-wrap {{
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    scrollbar-color: #33516d #08111d;
    scrollbar-width: thin;
  }}

  table {{
    width: 100%;
    min-width: 1240px;
    border-collapse: collapse;
    font-size: 13px;
    table-layout: fixed;
  }}

  .traction-section table {{
    min-width: 1340px;
  }}

  .col-repository {{ width: 20%; }}
  .col-language {{ width: 10%; }}
  .col-description {{ width: 52%; }}
  .col-github {{ width: 20%; }}
  .col-traction-repository {{ width: 16%; }}
  .col-traction-description {{ width: 50%; }}
  .col-traction-cluster {{ width: 12%; }}
  .col-traction-language {{ width: 8%; }}
  .col-traction-github {{ width: 14%; }}

  th {{
    position: sticky;
    top: 0;
    z-index: 1;
    padding: 13px 14px;
    text-align: left;
    background: var(--surface2);
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    font: 700 11px 'JetBrains Mono', monospace;
    text-transform: uppercase;
    letter-spacing: 0.7px;
  }}

  td {{
    padding: 13px 14px;
    vertical-align: top;
    border-bottom: 1px solid var(--border);
    line-height: 1.5;
  }}

  tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(16, 163, 127, 0.045); }}

  .repo-name {{
    display: flex;
    align-items: center;
    gap: 8px;
    font: 700 14px 'JetBrains Mono', monospace;
    white-space: nowrap;
  }}

  .repo-name a {{
    color: var(--text);
    text-decoration: none;
  }}

  .repo-name a:hover,
  .repo-slug:hover {{
    color: var(--openai);
    text-decoration: underline;
  }}

  .repo-slug {{
    display: block;
    margin-top: 4px;
    color: var(--text-muted);
    text-decoration: none;
    font: 500 11px 'JetBrains Mono', monospace;
  }}

  .avatar {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    background: var(--surface3);
  }}

  .tag,
  .badge {{
    display: inline-block;
    border-radius: 4px;
    padding: 2px 7px;
    font: 700 11px 'JetBrains Mono', monospace;
    background: rgba(149, 163, 154, 0.14);
    color: var(--text-muted);
  }}

  .tag-py {{ background: rgba(16, 163, 127, 0.14); color: var(--green); }}
  .tag-csharp {{ background: rgba(134, 97, 197, 0.18); color: var(--purple); }}
  .tag-c, .tag-cpp, .tag-cuda {{ background: rgba(69, 168, 255, 0.13); color: var(--blue); }}
  .tag-ts, .tag-js {{ background: rgba(250, 204, 21, 0.13); color: var(--yellow); }}
  .tag-go {{ background: rgba(32, 217, 199, 0.13); color: var(--cyan); }}
  .tag-shell {{ background: rgba(244, 114, 182, 0.13); color: var(--pink); }}

  .topic-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 7px;
  }}

  .content-profile {{
    margin-top: 9px;
    padding-top: 8px;
    border-top: 1px solid rgba(43, 44, 48, 0.78);
    color: var(--text-muted);
  }}

  .content-line {{
    display: grid;
    grid-template-columns: 54px minmax(0, 1fr);
    gap: 8px;
    align-items: start;
    margin-top: 4px;
    font-size: 11px;
    line-height: 1.45;
  }}

  .content-label {{
    color: var(--openai);
    font: 700 10px 'JetBrains Mono', monospace;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  .content-text {{
    min-width: 0;
  }}

  .content-paths {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }}

  .content-path,
  .content-badge {{
    display: inline-block;
    border: 1px solid rgba(43, 44, 48, 0.95);
    border-radius: 4px;
    padding: 1px 6px;
    background: rgba(8, 11, 13, 0.4);
    color: #c7d1cc;
    font: 700 10px 'JetBrains Mono', monospace;
    text-decoration: none;
  }}

  .content-path:hover {{
    color: var(--openai);
    border-color: var(--openai);
  }}

  .content-badge-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
  }}

  .content-badge {{
    color: var(--cyan);
    background: rgba(32, 217, 199, 0.08);
  }}

  .badge {{
    font-size: 10px;
    color: #c0d1e4;
  }}

  .keyword-button {{
    border: 0;
    cursor: pointer;
    text-align: left;
    appearance: none;
    -webkit-appearance: none;
    transition: color 0.15s ease, background 0.15s ease;
  }}

  .keyword-button:hover,
  .keyword-button:focus-visible {{
    color: var(--openai);
    background: rgba(16, 163, 127, 0.16);
    outline: none;
  }}

  .description-cell {{
    color: var(--text);
    max-width: 54rem;
  }}

  .github-cell {{
    min-width: 210px;
  }}

  .gh-stats {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px 10px;
    align-items: center;
  }}

  .star-count,
  .fork-count,
  .commit-count,
  .metric-sm {{
    font-family: 'JetBrains Mono', monospace;
  }}

  .star-count {{
    color: var(--yellow);
    font-weight: 800;
    font-size: 13px;
  }}

  .fork-count,
  .commit-count,
  .metric-sm {{
    color: var(--text-muted);
    font-size: 11px;
    font-weight: 600;
  }}

  .last-updated {{
    display: block;
    margin-top: 6px;
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
  }}

  .activity-block {{
    margin-top: 8px;
  }}

  .activity-bar {{
    height: 6px;
    border-radius: 999px;
    background: rgba(149, 163, 154, 0.16);
    overflow: hidden;
  }}

  .activity-bar span {{
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--openai), var(--green), var(--yellow));
  }}

  .activity-meta {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-top: 5px;
    color: var(--text-muted);
    font: 600 10px 'JetBrains Mono', monospace;
  }}

  .number-cell,
  .rank-cell,
  .date-cell {{
    font-family: 'JetBrains Mono', monospace;
    white-space: nowrap;
  }}

  .number-cell {{
    color: var(--text);
    font-weight: 700;
  }}

  .score-cell {{
    color: var(--openai);
  }}

  .date-cell {{
    color: var(--text-muted);
    font-size: 12px;
  }}

  .hidden {{
    display: none !important;
  }}

  .footer {{
    margin-top: 30px;
    color: var(--text-muted);
    font: 500 11px 'JetBrains Mono', monospace;
    line-height: 1.7;
  }}

  .footer a {{ color: var(--openai); text-decoration: none; }}
  .footer a:hover {{ text-decoration: underline; }}

  @media (max-width: 900px) {{
    body {{ padding: 26px 14px 40px; }}
    .page-title {{ font-size: 24px; }}
    .search-panel {{ grid-template-columns: 1fr; }}
    .button, .search-label, .sort-label {{ width: 100%; }}
    .section-header {{ display: block; }}
    .count-pill {{ margin-top: 10px; }}
  }}
</style>
</head>
<body>
  <header class="header page-header">
    <h1 class="page-title"><span class="logo-box">AI</span>OpenAI GitHub Repository Atlas</h1>
    <p>Searchable snapshot of public OpenAI repositories from <a href="https://github.com/orgs/openai/repositories" target="_blank" rel="noreferrer">github.com/openai</a>. Included repositories have more than 200 stars and a commit within the last three months, since {cutoff}.</p>
    <div class="metric-strip" aria-label="Catalog summary">
      <div class="metric-card"><strong>{len(repos)}</strong><span>qualified repos</span></div>
      <div class="metric-card"><strong>{fmt_number(total_stars)}</strong><span>combined stars</span></div>
      <div class="metric-card"><strong>{fmt_number(total_commits_30d)}</strong><span>commits in {args.traction_days}d</span></div>
      <div class="metric-card"><strong>{total_content_profiles}</strong><span>README profiles</span></div>
      <div class="metric-card"><strong>{len(CLUSTERS)}</strong><span>purpose clusters</span></div>
    </div>
    <nav class="jump-nav" aria-label="Section links">
      {nav_links}
    </nav>
  </header>

  <div class="search-panel" role="search">
    <div class="search-label">Search</div>
    <input id="global-table-search" class="search-input" type="search" autocomplete="off" spellcheck="false" placeholder="repo, topic, language, description">
    <button id="global-search-clear" class="button" type="button" disabled>Clear</button>
    <div class="sort-label">Sort by</div>
    <button id="sort-stars" class="button" type="button">Stars</button>
    <button id="sort-fresh" class="button active" type="button">Freshness</button>
  </div>

{traction}
{sections}
{extra_section_html}

  <footer class="footer">
    <p style="margin-bottom: 10px;">Useful links:
      <a class="gh-link" href="https://platform.openai.com/docs" target="_blank" rel="noreferrer" style="display: inline; margin-left: 6px;">Platform Docs</a>
      <span style="margin: 0 4px;">·</span>
      <a class="gh-link" href="https://platform.openai.com/docs/api-reference" target="_blank" rel="noreferrer" style="display: inline;">API Reference</a>
      <span style="margin: 0 4px;">·</span>
      <a class="gh-link" href="https://cookbook.openai.com/" target="_blank" rel="noreferrer" style="display: inline;">Cookbook</a>
      <span style="margin: 0 4px;">·</span>
      <a class="gh-link" href="https://openai.com/" target="_blank" rel="noreferrer" style="display: inline;">OpenAI</a>
    </p>
    <p style="text-align: center;">Generated from the GitHub API · Last updated: <span id="last-updated-date">{updated}</span></p>
  </footer>

<script>
  const searchInput = document.getElementById('global-table-search');
  const clearButton = document.getElementById('global-search-clear');
  const sortStars = document.getElementById('sort-stars');
  const sortFresh = document.getElementById('sort-fresh');

  function allRows() {{
    return Array.from(document.querySelectorAll('tbody tr'));
  }}

  function applySearch() {{
    const query = searchInput.value.trim().toLowerCase();
    let visibleRows = 0;
    document.querySelectorAll('[data-section]').forEach(section => {{
      let sectionVisible = false;
      section.querySelectorAll('tbody tr').forEach(row => {{
        const match = !query || row.textContent.toLowerCase().includes(query);
        row.classList.toggle('hidden', !match);
        if (match) {{
          sectionVisible = true;
          visibleRows += 1;
        }}
      }});
      section.classList.toggle('hidden', !sectionVisible);
    }});
    clearButton.disabled = !query;
    if (query && visibleRows === 0) {{
      document.querySelectorAll('[data-section], tbody tr').forEach(el => el.classList.remove('hidden'));
    }}
  }}

  function sortTables(kind) {{
    const attr = kind === 'fresh' ? 'pushed' : 'stars';
    document.querySelectorAll('tbody').forEach(tbody => {{
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => Number(b.dataset[attr] || 0) - Number(a.dataset[attr] || 0));
      rows.forEach(row => tbody.appendChild(row));
    }});
    sortStars.classList.toggle('active', kind === 'stars');
    sortFresh.classList.toggle('active', kind === 'fresh');
    applySearch();
  }}

  searchInput.addEventListener('input', applySearch);
  clearButton.addEventListener('click', () => {{
    searchInput.value = '';
    applySearch();
    searchInput.focus();
  }});
  document.addEventListener('click', event => {{
    const keyword = event.target.closest('[data-filter-keyword]');
    if (!keyword) return;
    const value = keyword.dataset.filterKeyword || keyword.textContent.trim();
    searchInput.value = searchInput.value.trim().toLowerCase() === value.trim().toLowerCase() ? '' : value;
    applySearch();
    searchInput.focus({{ preventScroll: true }});
  }});
  sortStars.addEventListener('click', () => sortTables('stars'));
  sortFresh.addEventListener('click', () => sortTables('fresh'));
  sortTables('fresh');
</script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    pushed_cutoff = subtract_months(now, args.months)
    traction_cutoff = now - timedelta(days=args.traction_days)

    token = get_token()
    if not token:
        print("warning: no GitHub token found; unauthenticated API rate limits may apply", file=sys.stderr)
    client = GitHubClient(token)
    repos = fetch_repositories(client, args.org, args.min_stars, pushed_cutoff)
    extra_sections = {
        section.key: fetch_extra_section_repositories(client, section, pushed_cutoff)
        for section in EXTRA_SECTIONS
    }
    all_repos = repos + [repo for section_repos in extra_sections.values() for repo in section_repos]
    if not args.skip_commit_counts:
        enrich_commit_counts(client, all_repos, traction_cutoff, pushed_cutoff)
        repos = [repo for repo in repos if int(repo.get("commits_since_cutoff") or 0) > 0]
        extra_sections = {
            key: [repo for repo in section_repos if int(repo.get("commits_since_cutoff") or 0) > 0]
            for key, section_repos in extra_sections.items()
        }
        all_repos = repos + [repo for section_repos in extra_sections.values() for repo in section_repos]
    if not args.skip_content:
        enrich_content_profiles(client, all_repos)

    history_path = Path(args.history)
    history = read_history(history_path)
    add_derived_fields(repos, history, now, args.traction_days)
    html = render_html(repos, extra_sections, now, pushed_cutoff, args)
    Path(args.file).write_text(html, encoding="utf-8")
    write_history(history_path, history, all_repos, now)
    print(f"Wrote {args.file} with {len(repos)} OpenAI repositories")
    print(f"Wrote {args.history}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
