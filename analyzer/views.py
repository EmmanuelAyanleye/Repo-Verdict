"""Django views for RepoVerdict."""
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .analyzer import FeatureAnalyzer
from .github_client import GitHubClient, GitHubClientError, GitHubRateLimitError, LICENSE_SLUG_MAP
from .models import AnalysisRequest


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

    # Default to repositories active within the last 12 months.
    pushed_after = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    # Deduplicate licenses by their GitHub slug so e.g. "Apache" and "Apache-2.0"
    # do not trigger two identical queries.
    allowed_slugs = {LICENSE_SLUG_MAP.get(lic, lic.lower().replace(" ", "-")) for lic in licenses}
    unique_licenses = sorted(
        {
            next(
                (lic for lic in licenses if LICENSE_SLUG_MAP.get(lic, lic.lower().replace(" ", "-")) == slug),
                slug,
            )
            for slug in allowed_slugs
        }
    )

    seen: set[str] = set()
    all_repos: list[dict] = []
    query_parts: list[str] = []

    try:
        client = GitHubClient()
        # GitHub search does not support OR between qualifiers, so we run one query
        # per language. If the license set is small we include it in the query for
        # better precision; otherwise we filter client-side.
        # We use max_pages=1 to stay well under GitHub's search rate limit
        # (30 requests/min for authenticated users, 10 for unauthenticated).
        use_combinations = len(languages) * len(unique_licenses) <= 6
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
                    repo_slug = repo_license.get("spdx_id")
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
        max_workers = 6
        enrich_candidates = all_repos[:15]
    else:
        # Unauthenticated rate limits are very tight; inspect fewer repos.
        max_workers = 3
        enrich_candidates = all_repos[:6]

    enriched: list[tuple[dict, dict]] = []

    def _fetch_repo_metrics(candidate: dict) -> tuple[dict, dict[str, int], list[dict]]:
        owner, repo_name = client.parse_repo_url(candidate["html_url"])
        languages = client.get_repo_languages(owner, repo_name)
        tree = client.get_repo_tree(owner, repo_name)
        return candidate, languages, tree

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_repo_metrics, r): r for r in enrich_candidates}
            for future in as_completed(futures):
                try:
                    repo, languages, tree = future.result()
                    metrics = _score_repo(
                        repo, languages, tree, min_loc, bool(require_architecture), bool(allow_surface)
                    )
                    enriched.append((repo, metrics))
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
    filtered: list[tuple[dict, dict]] = []
    for repo, metrics in enriched:
        if metrics["primary_loc"] < min_loc:
            continue
        if bool(require_architecture) and not metrics["architecture_ok"]:
            continue
        filtered.append((repo, metrics))

    # Sort by suitability score descending, then by stars as a tie-breaker.
    filtered.sort(
        key=lambda item: (item[1]["suitability_score"], item[0].get("stargazers_count", 0)),
        reverse=True,
    )

    # If strict suitability filters removed everything, fall back to the original star-ranked
    # list so the user still sees some candidates and can adjust criteria.
    if not filtered:
        filtered = [(r, _basic_repo_score(r)) for r in all_repos[:result_limit]]

    final_repos: list[dict] = []
    for repo, metrics in filtered[:result_limit]:
        repo = dict(repo)
        repo["suitability"] = metrics
        final_repos.append(repo)

    return Response(
        {
            "query": "; ".join(query_parts),
            "count": len(final_repos),
            "repositories": final_repos,
        }
    )
