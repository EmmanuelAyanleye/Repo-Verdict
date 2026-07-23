"""Django views for RepoVerdict."""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .analyzer import FeatureAnalyzer
from .github_client import GitHubClient, GitHubClientError, GitHubRateLimitError, LICENSE_SLUG_MAP
from .models import AnalysisRequest

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


# Heuristics for identifying repos that are likely too surface-only or architecture-light
# for multi-stage correctness tasks. These are used as penalties, never hard filters.
SURFACE_TERMS = [
    "frontend", "ui ", "ui-kit", "component library", "component suite", "admin panel",
    "admin dashboard", "dashboard", "crud", "scaffold", "boilerplate", "starter", "template",
    "etl", "crawler", "scraper", "web scraper", "cli tool", "command-line", "command line",
    "visualization", "chart", "plot", "portfolio", "blog", "cms", "static site", "landing page",
    "todo", "chatbot", "discord bot", "telegram bot", "bot framework", "wrapper",
    "sdk wrapper", "api client", "http client", "simple", "minimal", "tiny", "micro",
]

DEPTH_TERMS = [
    "framework", "engine", "compiler", "interpreter", "runtime", "middleware", "protocol",
    "parser", "validator", "serializer", "deserializer", "orm", "database", "server", "proxy",
    "router", "load balancer", "cache", "distributed", "kernel", "vm", "emulator", "simulator",
    "filesystem", "file system", "operating system", "toolkit", "library", "platform",
    "infrastructure", "service mesh", "message queue", "event bus", "workflow", "rules engine",
]

SOURCE_EXTENSIONS = {
    "Python": {".py"},
    "JavaScript": {".js", ".jsx", ".mjs", ".cjs"},
    "TypeScript": {".ts", ".tsx", ".mts", ".cts"},
    "Go": {".go"},
    "Rust": {".rs"},
    "C++": {".cpp", ".cc", ".cxx", ".h", ".hpp"},
    "Java": {".java"},
}

ALLOWED_LICENSE_SLUGS = {
    slug
    for name, slug in LICENSE_SLUG_MAP.items()
    if name != "Other" and slug != "other"
}

LICENSE_FILE_NAMES = {
    "license", "license.md", "license.txt", "license.rst",
    "copying", "copying.md", "copying.txt", "notice", "notice.txt",
}

ARCHITECTURE_DOC_HINTS = (
    "architecture", "design", "adr", "rfcs", "rfc", "docs/internals",
    "docs/design", "docs/architecture", "contributing",
)

BUILD_FILE_HINTS = {
    "Python": ("pyproject.toml", "poetry.lock", "requirements.txt", "setup.py", "setup.cfg"),
    "JavaScript": ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "TypeScript": ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "Go": ("go.mod", "go.sum"),
    "Rust": ("Cargo.toml", "Cargo.lock"),
    "C++": ("CMakeLists.txt", "vcpkg.json", "conanfile.txt", "conanfile.py"),
    "Java": ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"),
}

HEAVY_PATH_HINTS = (
    "vendor/", "vendors/", "third_party/", "third-party/", "node_modules/",
    "dist/", "build/", "generated/", "fixtures/", "dataset", "datasets/",
    ".onnx", ".pt", ".pth", ".bin", ".zip", ".tar", ".gz", ".mp4", ".png",
)


def _repo_text(repo: dict) -> str:
    """Combine repo name, description and topics into a single lowercase string."""
    parts = [
        repo.get("name") or "",
        repo.get("description") or "",
        " ".join(repo.get("topics", []) or []),
    ]
    return " ".join(p for p in parts if p).lower()


def _surface_penalty(text: str) -> int:
    """Penalize repos whose name/description/topics suggest surface-only behavior."""
    penalty = 0
    for term in SURFACE_TERMS:
        if term in text:
            penalty += 10
    # Extra penalty for words that usually mean IO wiring.
    for term in ["io", "pipeline", "connector", "adapter", "client", "bindings"]:
        if f" {term} " in f" {text} ":
            penalty += 3
    return min(penalty, 40)


def _depth_boost(text: str) -> int:
    """Boost repos whose name/description/topics suggest deep architectural work."""
    boost = 0
    for term in DEPTH_TERMS:
        if term in text:
            boost += 8
    return min(boost, 30)


def _parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _basic_repo_score(repo: dict) -> dict[str, Any]:
    """Return an honest metadata-only score when deep enrichment is unavailable."""
    stars = int(repo.get("stargazers_count") or 0)
    size = int(repo.get("size") or 0)
    pushed_at = _parse_github_datetime(repo.get("pushed_at"))
    days_old = (datetime.now() - pushed_at).days if pushed_at else None

    stars_score = min(35.0, math.log10(stars + 1) / 5 * 35)
    activity_score = 0.0
    if days_old is not None:
        activity_score = max(0.0, 25.0 * (1 - min(days_old, 365) / 365))
    metadata_score = 0.0
    if repo.get("language"):
        metadata_score += 15
    if repo.get("license"):
        metadata_score += 15
    if repo.get("description"):
        metadata_score += 10
    if size > 0:
        metadata_score += min(15, math.log10(size + 1) / 5 * 15)

    score = max(0.0, min(100.0, stars_score + activity_score + metadata_score))
    return {
        "primary_language": repo.get("language") or "",
        "primary_loc": 0,
        "source_file_count": 0,
        "source_dir_count": 0,
        "max_depth": 0,
        "surface_penalty": 0,
        "depth_boost": 0,
        "loc_score": 0,
        "architecture_score": 0,
        "suitability_score": round(score, 1),
        "architecture_ok": False,
        "score_mode": "basic",
        "score_available": True,
        "score_reason": "Basic score from stars, recency, license, language and repository metadata.",
    }


def _repo_size_mb(repo: dict) -> float:
    return round((repo.get("size") or 0) / 1024, 1)


def _license_allowed(repo: dict, allowed_slugs: set[str]) -> tuple[bool, str]:
    license_data = repo.get("license") or {}
    slug = (license_data.get("spdx_id") or "").lower()
    if not slug or slug == "noassertion":
        return False, "Root license is missing or not detected by GitHub."
    if slug not in allowed_slugs:
        return False, f"Root license {slug} is not in the allowed permissive list."
    return True, f"Root license {slug} is allowed."


def _tree_license_signal(tree: list[dict]) -> dict[str, Any]:
    license_paths: list[str] = []
    subpackage_license_paths: list[str] = []
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not path:
            continue
        name = os.path.basename(path).lower()
        if name in LICENSE_FILE_NAMES:
            license_paths.append(path)
            if "/" in path:
                subpackage_license_paths.append(path)
    if subpackage_license_paths:
        status = "subpackage license files need manual confirmation"
    elif license_paths:
        status = "root license file only"
    else:
        status = "no license file found in tree"
    return {
        "license_files": license_paths[:12],
        "subpackage_license_files": subpackage_license_paths[:12],
        "license_tree_status": status,
    }


def _path_dirs(filename: str) -> set[str]:
    parts = [p for p in filename.split("/") if p]
    if len(parts) <= 1:
        return {"."}
    return {parts[0], "/".join(parts[:2])}


def _coupling_from_prs(client: GitHubClient, owner: str, repo: str) -> dict[str, Any]:
    checked: list[dict[str, Any]] = []
    try:
        prs = client.get_recent_merged_pull_requests(owner, repo, limit=5)
    except Exception:
        prs = []

    for pr in prs[:5]:
        number = pr.get("number")
        if not number:
            continue
        checked.append({
            "number": number,
            "title": pr.get("title", ""),
            "url": pr.get("html_url", ""),
            "changed_files": pr.get("changed_files") or 0,
            "directory_count": 0,
            "directories": [],
        })

    broad_prs = [pr for pr in checked if pr["changed_files"] >= 6]
    if len(broad_prs) >= 2:
        status = "likely"
        sentence = (
            f"{len(broad_prs)}/{len(checked)} recent merged PRs changed 6+ files; "
            "directory coupling is unverified in fast search."
        )
    elif checked:
        status = "unverified"
        sentence = (
            f"Checked {len(checked)} recent merged PR summaries; Files changed directories not inspected in fast search."
        )
    else:
        status = "unverified"
        sentence = "unverified"
    return {
        "coupling_status": status,
        "coupling_sentence": sentence,
        "checked_pull_requests": checked,
    }


def _fast_coupling_from_tree(tree: list[dict], primary_language: str) -> dict[str, Any]:
    stats = _compute_tree_stats(tree, primary_language)
    source_dirs: set[str] = set()
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not path:
            continue
        if any(path.endswith(ext) for ext in SOURCE_EXTENSIONS.get(primary_language, set())):
            parts = path.split("/")
            if len(parts) > 1:
                source_dirs.add(parts[0])

    if len(source_dirs) >= 5 and stats["max_depth"] >= 2:
        status = "likely"
        sentence = (
            f"Likely: {stats['source_file_count']} {primary_language} files across "
            f"{len(source_dirs)} top-level source areas; examples include {', '.join(sorted(source_dirs)[:5])}."
        )
    elif len(source_dirs) >= 2:
        status = "weak"
        sentence = (
            f"Weak/moderate: source spans {len(source_dirs)} top-level areas, but PR file coupling is not verified."
        )
    else:
        status = "unverified"
        sentence = "Unverified: source tree does not show broad component spread in fast search."

    return {
        "coupling_status": status,
        "coupling_sentence": sentence,
        "checked_pull_requests": [],
    }


def _reliability_signals(
    client: GitHubClient,
    owner: str,
    repo: str,
    repo_data: dict,
    tree: list[dict],
    primary_language: str,
) -> dict[str, Any]:
    paths = [entry.get("path", "") for entry in tree if entry.get("path")]
    lower_paths = [p.lower() for p in paths]

    build_files = [
        p for p in paths
        if os.path.basename(p) in BUILD_FILE_HINTS.get(primary_language, ())
        or os.path.basename(p) in {".github", "dockerfile"}
    ][:12]
    has_submodules = any(entry.get("type") == "commit" for entry in tree)
    heavy_paths = [p for p in lower_paths if any(hint in p for hint in HEAVY_PATH_HINTS)][:10]
    docs = [p for p in lower_paths if any(hint in p for hint in ARCHITECTURE_DOC_HINTS)][:10]

    try:
        runs = client.get_recent_workflow_runs(owner, repo, limit=4)
    except Exception:
        runs = []
    ci_failures = [r for r in runs if r.get("conclusion") not in ("success", "skipped")]

    risks: list[str] = []
    if not build_files:
        risks.append("No standard build manifest detected in repository tree.")
    if has_submodules:
        risks.append("Git submodules detected; pinned checkout may be more fragile.")
    if heavy_paths:
        risks.append("Possible vendored/generated/heavy paths detected.")
    if ci_failures:
        risks.append(f"{len(ci_failures)}/{len(runs)} recent CI runs did not pass.")
    if not docs:
        risks.append("No obvious architecture/design/ADR docs found.")
    if not risks:
        risks.append("No major reliability risks detected from bounded metadata checks.")

    return {
        "build_files": build_files,
        "has_submodules": has_submodules,
        "heavy_paths": heavy_paths,
        "architecture_docs": docs,
        "ci_checked": len(runs),
        "ci_successes": len([r for r in runs if r.get("conclusion") == "success"]),
        "reliability_risks": risks,
    }


def _fast_reliability_signals(repo_data: dict, tree: list[dict], primary_language: str) -> dict[str, Any]:
    paths = [entry.get("path", "") for entry in tree if entry.get("path")]
    lower_paths = [p.lower() for p in paths]
    build_files = [
        p for p in paths
        if os.path.basename(p) in BUILD_FILE_HINTS.get(primary_language, ())
        or os.path.basename(p).lower() in {"dockerfile", "compose.yaml", "compose.yml"}
    ][:12]
    docs = [p for p in lower_paths if any(hint in p for hint in ARCHITECTURE_DOC_HINTS)][:10]
    heavy_paths = [p for p in lower_paths if any(hint in p for hint in HEAVY_PATH_HINTS)][:10]
    has_submodules = any(entry.get("type") == "commit" for entry in tree)

    risks: list[str] = []
    if not build_files:
        risks.append("No standard build manifest found in fast tree scan.")
    if heavy_paths:
        risks.append(f"Possible heavy/generated paths: {', '.join(heavy_paths[:3])}.")
    if has_submodules:
        risks.append("Git submodules detected.")
    if not docs:
        risks.append("No architecture/design/ADR docs found in fast tree scan.")
    if repo_data.get("archived"):
        risks.append("Repository is archived.")
    if not risks:
        risks.append("Fast tree scan found standard build files and no obvious heavy/submodule risks.")

    return {
        "build_files": build_files,
        "has_submodules": has_submodules,
        "heavy_paths": heavy_paths,
        "architecture_docs": docs,
        "ci_checked": 0,
        "ci_successes": 0,
        "reliability_risks": risks,
    }


def _fast_gap_ideas(repo: dict, tree: list[dict], primary_language: str) -> list[str]:
    paths = [entry.get("path", "").lower() for entry in tree if entry.get("path")]
    top_dirs = {
        path.split("/", 1)[0]
        for path in paths
        if "/" in path and any(path.endswith(ext) for ext in SOURCE_EXTENSIONS.get(primary_language, set()))
    }
    text = " ".join([
        _repo_text(repo),
        " ".join(paths[:250]),
        " ".join(sorted(top_dirs)),
    ])

    if any(term in text for term in ("compiler", "interpreter", "parser", "lexer", "ast", "typecheck", "type_checker", "semantic")):
        return [
            "Investigate parser, AST, or type-system edge cases where the obvious implementation can mis-handle syntax/semantics.",
            "Look for diagnostics, incremental analysis, or language-feature issues touching parser/checker/codegen modules.",
        ]
    if any(term in text for term in ("database", "storage", "query", "sql", "planner", "optimizer", "catalog", "transaction")):
        return [
            "Investigate planner/optimizer correctness gaps across parser, catalog, execution, and storage paths.",
            "Look for transaction, serialization, schema-evolution, or predicate-pushdown edge cases.",
        ]
    if any(term in text for term in ("distributed", "consensus", "raft", "replication", "cluster", "scheduler", "queue", "broker")):
        return [
            "Investigate consistency, ordering, retry, or failover behavior across scheduler/replication/state modules.",
            "Look for race-condition, recovery, or backpressure issues that require multi-component fixes.",
        ]
    if any(term in text for term in ("agent", "llm", "workflow", "chain", "retriever", "tool", "memory", "prompt")):
        return [
            "Investigate agent state, tool-call, retrieval, or memory behavior gaps across orchestration and model adapters.",
            "Look for retry, streaming, structured-output, or context-window issues with cross-module impact.",
        ]
    if any(term in text for term in ("serializer", "schema", "validator", "protobuf", "json", "encoding", "decoder")):
        return [
            "Investigate schema/serialization edge cases where round-trip or validation invariants can break.",
            "Look for compatibility, coercion, or nested-structure behavior spanning parser/model/output modules.",
        ]
    if top_dirs:
        sample = ", ".join(sorted(top_dirs)[:4])
        return [
            f"Investigate behavior that crosses these source areas: {sample}.",
            "Skim recent issues for bugs involving interactions between those modules rather than one-file changes.",
        ]
    return [
        "Use as a volume candidate only: fast scan found limited domain signals.",
        "Manually inspect open issues/discussions before treating it as a strong task base.",
    ]


def _issue_gap_ideas(client: GitHubClient, owner: str, repo: str) -> list[str]:
    ideas: list[str] = []
    try:
        issues = client.get_issues(owner, repo, state="open")[:20]
    except Exception:
        issues = []
    useful_labels = ("bug", "enhancement", "feature", "design", "discussion", "proposal")
    for issue in issues:
        labels = [label.get("name", "").lower() for label in issue.get("labels", [])]
        if labels and not any(any(key in label for key in useful_labels) for label in labels):
            continue
        title = issue.get("title")
        if title:
            ideas.append(f"Investigate issue #{issue.get('number')}: {title}")
        if len(ideas) >= 2:
            break
    return ideas or ["Skim open design/bug issues for behavior gaps worth validating."]


def _domain_boost(repo: dict, domain_ideas: str) -> float:
    if not domain_ideas:
        return 0.0
    text = _repo_text(repo)
    terms = [
        term.strip().lower()
        for term in re.split(r"[,;\n]+", domain_ideas)
        if len(term.strip()) > 2
    ]
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term in text)
    return min(10.0, hits * 5.0)


def _shipd_candidate_score(
    repo: dict,
    metrics: dict[str, Any],
    coupling: dict[str, Any],
    reliability: dict[str, Any],
    domain_ideas: str = "",
) -> float:
    score = float(metrics.get("suitability_score", 0))
    if coupling["coupling_status"] == "verified":
        score += 25
    elif coupling["coupling_status"] == "likely":
        score += 10
    elif coupling["coupling_status"] == "weak":
        score -= 10
    else:
        score -= 20
    score -= min(25, max(0, len(reliability["reliability_risks"]) - 1) * 6)
    if reliability["architecture_docs"]:
        score += 8
    if reliability["ci_checked"] and reliability["ci_successes"] == reliability["ci_checked"]:
        score += 7
    score += _domain_boost(repo, domain_ideas)
    return round(max(0, min(100, score)), 1)


def _long_horizon_fit(
    repo: dict,
    metrics: dict[str, Any],
    reliability: dict[str, Any],
    domain_ideas: str = "",
) -> dict[str, Any]:
    """Score whether a repo can sustain deep, multi-file Shipd tasks."""
    loc = int(metrics.get("primary_loc") or 0)
    files = int(metrics.get("source_file_count") or 0)
    dirs = int(metrics.get("source_dir_count") or 0)
    depth = int(metrics.get("max_depth") or 0)
    clone_size = float(metrics.get("approx_clone_size_mb") or _repo_size_mb(repo))
    open_issues = int(repo.get("open_issues_count") or 0)
    text = _repo_text(repo)

    docs = reliability.get("architecture_docs") or []
    build_files = reliability.get("build_files") or []
    heavy_paths = reliability.get("heavy_paths") or []
    has_submodules = bool(reliability.get("has_submodules"))
    topic_hits = len(repo.get("topics", []) or [])

    depth_score = min(30, (loc / 150000 * 12) + min(8, files / 250 * 8) + min(6, dirs / 25 * 6) + min(4, depth))
    backlog_score = min(25, open_issues / 20 * 25)

    surface_penalty = _surface_penalty(text)
    low_api_score = max(0, 20 - min(18, surface_penalty / 2))
    if metrics.get("depth_boost", 0) >= 16:
        low_api_score = min(20, low_api_score + 4)

    uniqueness_score = min(15, (metrics.get("depth_boost", 0) / 30 * 9) + _domain_boost(repo, domain_ideas) + min(3, topic_hits / 4))
    health_score = 0
    if build_files:
        health_score += 4
    if docs:
        health_score += 3
    if repo.get("description"):
        health_score += 1
    if not repo.get("archived"):
        health_score += 1
    if not heavy_paths and not has_submodules:
        health_score += 1
    health_score = min(10, health_score)

    sections = [
        {
            "name": "Codebase depth",
            "score": round(depth_score, 1),
            "max": 30,
            "detail": f"{loc:,} LOC, {files:,} source files, {dirs:,} dirs, depth {depth}",
        },
        {
            "name": "Feature backlog",
            "score": round(backlog_score, 1),
            "max": 25,
            "detail": f"{open_issues:,} open issues/discussions signal to inspect for task leads",
        },
        {
            "name": "Low API surface",
            "score": round(low_api_score, 1),
            "max": 20,
            "detail": "Penalizes UI, CRUD, wrapper, template, and glue signals in repo metadata.",
        },
        {
            "name": "Uniqueness",
            "score": round(uniqueness_score, 1),
            "max": 15,
            "detail": "Rewards compilers, engines, planners, storage, schemas, distributed systems, and matching domain hints.",
        },
        {
            "name": "Health & prose-fit",
            "score": round(health_score, 1),
            "max": 10,
            "detail": "Checks build manifests, docs/ADR signals, archive status, heavy paths, and submodules.",
        },
    ]
    total = round(sum(section["score"] for section in sections), 1)

    blockers: list[str] = []
    cautions: list[str] = []
    if clone_size > 60:
        blockers.append(f"Clone size is about {clone_size} MB, above the 60MB ceiling.")
    if repo.get("archived"):
        blockers.append("Repository is archived.")
    if not build_files:
        cautions.append("No standard build manifest detected in fast scan.")
    if not docs:
        cautions.append("No architecture/design/ADR docs found in fast scan.")
    if heavy_paths:
        cautions.append("Possible heavy/generated/vendor paths need manual review.")
    if has_submodules:
        cautions.append("Git submodules detected.")
    if metrics.get("coupling_status") not in {"verified", "likely"}:
        cautions.append("Cross-component coupling needs PR-level verification.")

    if blockers:
        decision = "REJECT"
        label = "Blocked"
    elif total >= 70:
        decision = "APPROVE"
        label = "Strong score"
    elif total >= 50:
        decision = "REVIEW"
        label = "Needs review"
    else:
        decision = "REJECT"
        label = "Weak fit"

    supports_multifile = "yes" if dirs >= 5 and files >= 50 else "maybe"
    tool_calls = "likely" if loc >= 30000 and dirs >= 5 else "unclear"
    prose = "good" if docs else "thin"
    return {
        "decision": decision,
        "label": label,
        "total": total,
        "blockers": blockers,
        "cautions": cautions,
        "sections": sections,
        "target_check": (
            f"Task capacity: {supports_multifile} for a 550-LOC, 3+ file change. "
            f"Deep investigation runway: {tool_calls}. Spec clarity signal: {prose}."
        ),
    }


def _llm_refine_search_results(
    repos: list[dict],
    language: str,
    domain_ideas: str,
) -> tuple[list[dict], bool]:
    """Use OpenAI to rerank and explain search results without changing hard filters."""
    _llm_refine_search_results.last_error = ""
    if not repos or not OpenAI or not settings.OPENAI_API_KEY:
        return repos, False

    compact = []
    for repo in repos[:20]:
        s = repo.get("suitability", {})
        compact.append({
            "full_name": repo.get("full_name"),
            "description": repo.get("description"),
            "topics": repo.get("topics", [])[:12],
            "stars": repo.get("stargazers_count"),
            "language": s.get("primary_language") or repo.get("language"),
            "loc": s.get("primary_loc"),
            "clone_size_mb": s.get("approx_clone_size_mb"),
            "source_dirs": s.get("source_dir_count"),
            "source_files": s.get("source_file_count"),
            "depth": s.get("max_depth"),
            "base_score": s.get("suitability_score"),
            "coupling": s.get("coupling_sentence"),
            "risks": s.get("reliability_risks", [])[:4],
        })

    prompt = f"""Rank these GitHub repositories for a Shipd Olympus challenge base.
Hard filters have already been applied. Do not invent facts and do not add repos.

Target language: {language}
Domain ideas: {domain_ideas or "none"}

Prefer repositories with implementation-level correctness work, non-trivial invariants,
cross-component behavior, standard build signals, and low reliability risk.
Avoid UI-only, CRUD, wrappers, glue, and repos with unclear task boundaries.

Return JSON only:
{{
  "rankings": [
    {{
      "full_name": "owner/repo",
      "ai_score": 0-100,
      "coupling_sentence": "one concrete sentence based on provided metadata",
      "reliability_risks": ["1-3 concise risks or checks"],
      "feature_gap_ideas": ["1-2 concrete investigation leads"]
    }}
  ]
}}

Repositories:
{json.dumps(compact, ensure_ascii=False)}
"""
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30.0)
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise repository triage assistant. Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1800,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        data = json.loads(content.strip())
        rankings = data.get("rankings", [])
    except Exception as exc:
        _llm_refine_search_results.last_error = exc.__class__.__name__
        return repos, False

    by_name = {repo.get("full_name"): repo for repo in repos}
    ordered: list[dict] = []
    seen: set[str] = set()
    for item in rankings:
        full_name = item.get("full_name")
        repo = by_name.get(full_name)
        if not repo:
            continue
        s = repo.setdefault("suitability", {})
        ai_score = item.get("ai_score")
        try:
            ai_score = max(0, min(100, float(ai_score)))
        except (TypeError, ValueError):
            ai_score = s.get("suitability_score", 0)
        s["ai_score"] = round(ai_score, 1)
        s["suitability_score"] = round(ai_score, 1)
        s["score_mode"] = "ai"
        s["score_reason"] = "OpenAI-refined Shipd fit score from GitHub metadata and fast repository signals."
        if item.get("coupling_sentence"):
            s["coupling_sentence"] = item["coupling_sentence"]
        if item.get("reliability_risks"):
            s["reliability_risks"] = item["reliability_risks"][:3]
        if item.get("feature_gap_ideas"):
            s["feature_gap_ideas"] = item["feature_gap_ideas"][:2]
        ordered.append(repo)
        seen.add(full_name)

    for repo in repos:
        if repo.get("full_name") not in seen:
            ordered.append(repo)
    return ordered, bool(ordered)


_llm_refine_search_results.last_error = ""


def _compute_tree_stats(tree: list[dict], primary_language: str | None) -> dict[str, Any]:
    """Return structural stats from a GitHub tree: dirs, source files, max depth."""
    extensions = SOURCE_EXTENSIONS.get(primary_language, set()) if primary_language else set()
    dirs: set[str] = set()
    source_files: list[str] = []
    max_depth = 0

    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not path:
            continue
        if extensions and any(path.endswith(ext) for ext in extensions):
            source_files.append(path)
            dir_part = os.path.dirname(path)
            if dir_part:
                dirs.add(dir_part)
            depth = path.count("/")
            if depth > max_depth:
                max_depth = depth

    return {
        "source_file_count": len(source_files),
        "source_dir_count": len(dirs),
        "max_depth": max_depth,
    }


def _score_repo(
    repo: dict,
    languages: dict[str, int],
    tree: list[dict],
    min_loc: int,
    require_architecture: bool,
    allow_surface: bool,
) -> dict[str, Any]:
    """Compute suitability metadata and score for a repository.

    Score breakdown (0-100):
      - LOC score: up to 50 points (full credit at/above min_loc)
      - Architecture score: up to 30 points from file tree depth/diversity
      - Depth boost: up to 30 points from name/description depth signals
      - Surface penalty: up to 40 points removed for surface-only signals
    """
    primary_language = repo.get("language") or ""
    if languages and primary_language and primary_language not in languages:
        primary_language = max(languages, key=languages.get)
    loc = languages.get(primary_language, 0) if languages else 0

    # LOC score: ramp from 0 to 50 as LOC approaches min_loc, small bonus above.
    if loc <= 0:
        loc_score = 0
    elif loc >= min_loc:
        loc_score = 50 + min(10, (loc - min_loc) / 100000 * 10)
    else:
        loc_score = (loc / min_loc) * 50

    text = _repo_text(repo)
    surface_penalty = 0 if allow_surface else _surface_penalty(text)
    depth_boost = _depth_boost(text)

    tree_stats = _compute_tree_stats(tree, primary_language)
    file_count = tree_stats["source_file_count"]
    dir_count = tree_stats["source_dir_count"]
    max_depth = tree_stats["max_depth"]

    # Architecture score: reward diverse directories, deep trees, and many source files.
    depth_score = (
        min(12, dir_count / 5 * 3)
        + min(10, max_depth * 2)
        + min(8, file_count / 25)
    )
    depth_score = min(30, depth_score)

    # Hard architecture requirement: if the user wants substantial structure, fail repos
    # that are effectively flat or tiny even if they have enough LOC.
    if require_architecture:
        if dir_count < 2 or file_count < 8 or max_depth < 1:
            architecture_ok = False
        else:
            architecture_ok = True
    else:
        architecture_ok = True

    score = loc_score + depth_score - surface_penalty + depth_boost
    score = max(0, min(100, score))

    return {
        "primary_language": primary_language,
        "primary_loc": loc,
        "source_file_count": file_count,
        "source_dir_count": dir_count,
        "max_depth": max_depth,
        "surface_penalty": surface_penalty,
        "depth_boost": depth_boost,
        "loc_score": round(loc_score, 1),
        "architecture_score": round(depth_score, 1),
        "suitability_score": round(score, 1),
        "architecture_ok": architecture_ok,
        "score_mode": "full",
        "score_available": True,
        "score_reason": "Full score from language LOC, file-tree architecture and repository metadata.",
    }


@require_GET
def index(request):
    return render(request, "analyzer/index.html")


@require_GET
def search(request):
    return render(request, "analyzer/search.html")


@csrf_exempt
@require_POST
@api_view(["POST"])
def analyze(request):
    try:
        payload = request.data if hasattr(request, "data") else json.loads(request.body)
    except json.JSONDecodeError:
        return Response({"error": "Invalid JSON body."}, status=400)

    repo_url = payload.get("repo_url", "").strip()
    commit_hash = payload.get("commit_hash", "").strip()
    feature = payload.get("feature", {}) or {}

    if not repo_url or not commit_hash:
        return Response(
            {"error": "Both repo_url and commit_hash are required."},
            status=400,
        )

    try:
        owner, repo = FeatureAnalyzer().client.parse_repo_url(repo_url)
    except GitHubClientError as exc:
        return Response({"error": str(exc)}, status=400)

    try:
        analyzer = FeatureAnalyzer()
        result = analyzer.analyze(repo_url, commit_hash, feature)
    except GitHubRateLimitError as exc:
        return Response({"error": str(exc)}, status=429)
    except GitHubClientError as exc:
        return Response({"error": str(exc)}, status=400)
    except Exception as exc:
        return Response({"error": f"Analysis failed: {str(exc)}"}, status=500)

    # Persist analysis
    AnalysisRequest.objects.create(
        repo_url=repo_url,
        owner=owner,
        repo=repo,
        commit_hash=commit_hash,
        feature_title=feature.get("title", ""),
        feature_category=feature.get("category", ""),
        verdict=result.verdict,
        confidence=result.confidence,
        summary=result.summary,
    )

    response_data = {
        "verdict": result.verdict,
        "confidence": result.confidence,
        "summary": result.summary,
        "feature_keywords": result.feature_keywords,
        "evidence": {
            "issues": [asdict(i) for i in result.evidence["issues"]],
            "pull_requests": [asdict(i) for i in result.evidence["pull_requests"]],
            "commits": [asdict(i) for i in result.evidence["commits"]],
            "branches": [asdict(i) for i in result.evidence["branches"]],
        },
        "details": result.details,
        "ai_review": result.ai_review,
    }

    return Response(response_data)


@csrf_exempt
@require_POST
@api_view(["POST"])
def search_repos(request):
    try:
        payload = request.data if hasattr(request, "data") else json.loads(request.body)
    except json.JSONDecodeError:
        return Response({"error": "Invalid JSON body."}, status=400)

    visibility = payload.get("visibility", "public")
    languages = payload.get("languages", []) or []
    min_stars = payload.get("min_stars", 500)
    max_stars = payload.get("max_stars")
    max_size_mb = payload.get("max_size_mb")
    result_limit = payload.get("result_limit", 30)
    licenses = payload.get("licenses", []) or []
    domain_ideas = (payload.get("domain_ideas") or "").strip()
    shipd_mode = payload.get("shipd_mode", True)

    # Suitability criteria.
    min_loc = payload.get("min_loc", 30000)
    require_architecture = payload.get("require_architecture", True)
    allow_surface = payload.get("allow_surface", False)

    if not languages:
        return Response({"error": "Select at least one language."}, status=400)
    if not licenses:
        return Response({"error": "Select at least one license."}, status=400)

    try:
        result_limit = min(int(result_limit), 100)
    except (ValueError, TypeError):
        result_limit = 30

    try:
        min_loc = max(0, int(min_loc or 0))
    except (ValueError, TypeError):
        min_loc = 30000
    if bool(shipd_mode):
        visibility = "public"
        try:
            min_stars = max(500, int(min_stars or 500))
        except (ValueError, TypeError):
            min_stars = 500
        min_loc = max(30000, min_loc)
        if max_size_mb is None:
            max_size_mb = 60
        result_limit = min(max(result_limit, 10), 20)

    # Default to repositories active within the last 12 months.
    pushed_after = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    # Deduplicate licenses by their GitHub slug so e.g. "Apache" and "Apache-2.0"
    # do not trigger two identical queries.
    allowed_slugs = {
        LICENSE_SLUG_MAP.get(lic, lic.lower().replace(" ", "-")).lower()
        for lic in licenses
    }
    if bool(shipd_mode):
        allowed_slugs = allowed_slugs & ALLOWED_LICENSE_SLUGS
        if not allowed_slugs:
            return Response({"error": "Select at least one allowed permissive license."}, status=400)
    unique_licenses = sorted(allowed_slugs)

    seen: set[str] = set()
    all_repos: list[dict] = []
    query_parts: list[str] = []

    try:
        client = GitHubClient()
        # GitHub repository search has a tight dedicated rate limit (30/minute
        # when authenticated), so Shipd mode uses one query per language and
        # applies the selected license list locally from GitHub's SPDX metadata.
        use_combinations = False if bool(shipd_mode) else len(languages) * len(unique_licenses) <= 3
        for language in languages:
            if use_combinations:
                for license in unique_licenses:
                    query, items = client.search_repositories(
                        visibility=visibility,
                        language=language,
                        min_stars=min_stars,
                        max_stars=max_stars,
                        pushed_after=pushed_after,
                        license=license,
                        max_pages=1,
                    )
                    query_parts.append(query)
                    for repo in items:
                        if repo.get("id") in seen:
                            continue
                        seen.add(repo.get("id"))
                        all_repos.append(repo)
                    # Short pacing to avoid search API burst limits.
                    time.sleep(0.25)
            else:
                query, items = client.search_repositories(
                    visibility=visibility,
                    language=language,
                    min_stars=min_stars,
                    max_stars=max_stars,
                    pushed_after=pushed_after,
                    max_pages=1,
                )
                query_parts.append(query)
                for repo in items:
                    repo_license = repo.get("license") or {}
                    repo_slug = (repo_license.get("spdx_id") or "").lower()
                    if not repo_slug or repo_slug not in allowed_slugs:
                        continue
                    if repo.get("id") in seen:
                        continue
                    seen.add(repo.get("id"))
                    all_repos.append(repo)
                time.sleep(0.25)
    except GitHubRateLimitError as exc:
        return Response({"error": str(exc)}, status=429)
    except GitHubClientError as exc:
        return Response({"error": str(exc)}, status=400)
    except Exception as exc:
        return Response({"error": f"Search failed: {str(exc)}"}, status=500)

    # Client-side filters for max stars and max repo size.
    if bool(shipd_mode):
        all_repos = [r for r in all_repos if not r.get("private")]
        all_repos = [r for r in all_repos if (r.get("stargazers_count") or 0) >= 500]
        cutoff = datetime.now() - timedelta(days=365)
        all_repos = [
            r for r in all_repos
            if (_parse_github_datetime(r.get("pushed_at")) or datetime.min) >= cutoff
        ]
        license_filtered = []
        for repo in all_repos:
            ok, _ = _license_allowed(repo, allowed_slugs)
            if ok:
                license_filtered.append(repo)
        all_repos = license_filtered

    if max_stars is not None:
        try:
            max_stars = int(max_stars)
            all_repos = [r for r in all_repos if r.get("stargazers_count", 0) <= max_stars]
        except (ValueError, TypeError):
            pass
    if max_size_mb is not None:
        try:
            max_size_kb = int(max_size_mb) * 1024
            all_repos = [r for r in all_repos if (r.get("size") or 0) <= max_size_kb]
        except (ValueError, TypeError):
            pass

    # Enrich and score top candidates. Each repo needs two extra API calls (languages
    # and tree), so we keep the pool small to avoid rate limits and long waits.
    all_repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    if client.token:
        max_workers = 8
        enrich_candidates = all_repos[:max(result_limit * 3, 30) if bool(shipd_mode) else 15]
    else:
        # Unauthenticated rate limits are very tight; inspect fewer repos.
        max_workers = 4
        enrich_candidates = all_repos[:max(result_limit * 2, 20) if bool(shipd_mode) else 6]

    enriched: list[tuple[dict, dict, list[dict]]] = []

    def _fetch_repo_metrics(candidate: dict) -> tuple[dict, dict[str, int], list[dict]]:
        owner, repo_name = client.parse_repo_url(candidate["html_url"])
        language_stats = client.get_repo_languages(owner, repo_name)
        tree = client.get_repo_tree(owner, repo_name)
        return candidate, language_stats, tree

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_repo_metrics, r): r for r in enrich_candidates}
            for future in as_completed(futures):
                try:
                    repo, language_stats, tree = future.result()
                    metrics = _score_repo(
                        repo, language_stats, tree, min_loc, bool(require_architecture), bool(allow_surface)
                    )
                    enriched.append((repo, metrics, tree))
                except Exception:
                    # If enrichment fails for a repo, skip it silently.
                    continue
    except GitHubRateLimitError as exc:
        return Response({"error": str(exc)}, status=429)
    except GitHubClientError as exc:
        return Response({"error": str(exc)}, status=400)
    except Exception as exc:
        return Response({"error": f"Search failed: {str(exc)}"}, status=500)

    # Apply suitability filters.
    filtered: list[tuple[dict, dict, list[dict]]] = []
    for repo, metrics, tree in enriched:
        if bool(shipd_mode):
            if metrics["primary_language"] not in languages:
                continue
            if metrics["primary_loc"] < min_loc:
                continue
            if not metrics["architecture_ok"]:
                continue
        else:
            if metrics["primary_loc"] < min_loc:
                continue
            if bool(require_architecture) and not metrics["architecture_ok"]:
                continue
        filtered.append((repo, metrics, tree))

    # Sort by suitability score descending, then by stars as a tie-breaker.
    filtered.sort(
        key=lambda item: (item[1]["suitability_score"], item[0].get("stargazers_count", 0)),
        reverse=True,
    )

    # In non-Shipd mode, fall back to metadata scores so the user still sees
    # something. In Shipd mode, do not suggest repos that fail hard requirements.
    if not filtered and not bool(shipd_mode):
        filtered = [(r, _basic_repo_score(r), []) for r in all_repos[:result_limit]]

    final_repos: list[dict] = []
    if bool(shipd_mode):
        detailed: list[tuple[dict, dict, list[dict], float]] = []
        for repo, metrics, tree in filtered:
            license_tree = _tree_license_signal(tree)
            if license_tree["subpackage_license_files"]:
                continue
            coupling = _fast_coupling_from_tree(tree, metrics["primary_language"])
            reliability = _fast_reliability_signals(repo, tree, metrics["primary_language"])
            metrics.update(license_tree)
            metrics.update(coupling)
            metrics.update(reliability)
            metrics["feature_gap_ideas"] = _fast_gap_ideas(repo, tree, metrics["primary_language"])
            metrics["approx_clone_size_mb"] = _repo_size_mb(repo)
            metrics["shipd_score"] = _shipd_candidate_score(
                repo,
                metrics,
                coupling,
                reliability,
                domain_ideas,
            )
            metrics["long_horizon_fit"] = _long_horizon_fit(repo, metrics, reliability, domain_ideas)
            metrics["suitability_score"] = metrics["shipd_score"]
            metrics["score_reason"] = "Fast Shipd fit score from hard filters, LOC, tree architecture, docs/build risk and repo metadata."
            detailed.append((repo, metrics, tree, metrics["shipd_score"]))
        detailed.sort(
            key=lambda item: (item[3], item[0].get("stargazers_count", 0)),
            reverse=True,
        )
        filtered = [(repo, metrics, tree) for repo, metrics, tree, _ in detailed]

    for repo, metrics, _tree in filtered[:result_limit]:
        repo = dict(repo)
        repo["suitability"] = metrics
        final_repos.append(repo)

    ai_enhanced = False
    if bool(shipd_mode) and final_repos:
        target_language = ", ".join(languages) if isinstance(languages, list) else str(languages)
        final_repos, ai_enhanced = _llm_refine_search_results(
            final_repos,
            target_language,
            domain_ideas,
        )
    ai_error = getattr(_llm_refine_search_results, "last_error", "")

    size_summary = f"<= {max_size_mb}MB" if max_size_mb is not None else "any size"
    summary = (
        f"Shipd search across {', '.join(languages)}: public repos, "
        f"{min_stars}+ stars, pushed since {pushed_after}, "
        f"{min_loc:,}+ target-language LOC, {size_summary}, "
        f"{len(unique_licenses)} allowed license filters."
    )

    return Response(
        {
            "query": summary,
            "summary": summary,
            "raw_queries": query_parts,
            "count": len(final_repos),
            "ai_enhanced": ai_enhanced,
            "ai_error": ai_error if not ai_enhanced else "",
            "repositories": final_repos,
        }
    )
