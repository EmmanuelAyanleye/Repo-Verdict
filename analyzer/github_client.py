"""GitHub API client for RepoVerdict."""
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class GitHubClientError(Exception):
    pass


class GitHubRateLimitError(GitHubClientError):
    pass


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str | None = None):
        self.token = token or settings.GITHUB_TOKEN
        self.timeout = getattr(settings, "GITHUB_TIMEOUT", 30)
        self.max_pages = getattr(settings, "GITHUB_MAX_PAGES", 3)
        self.session = requests.Session()

        # Retry on transient failures and read/connect timeouts.
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["X-GitHub-Api-Version"] = "2022-11-28"

    @staticmethod
    def parse_repo_url(repo_url: str) -> tuple[str, str]:
        """Extract (owner, repo) from a GitHub URL."""
        parsed = urlparse(repo_url.strip().rstrip("/"))
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise GitHubClientError(
                "Invalid GitHub repository URL. Expected format: https://github.com/owner/repo"
            )
        owner, repo = path_parts[0], path_parts[1]
        repo = repo.replace(".git", "")
        return owner, repo

    def _request(self, endpoint: str, params: dict | None = None) -> requests.Response:
        url = f"{self.BASE_URL}{endpoint}" if endpoint.startswith("/") else endpoint
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise GitHubClientError(
                f"GitHub API request timed out after {self.timeout}s. "
                "Your network may be slow or unstable. Try again or increase GITHUB_TIMEOUT."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise GitHubClientError(
                "Could not connect to api.github.com. Check your internet connection / DNS / proxy."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise GitHubClientError(
                f"GitHub request failed: {str(exc)}"
            ) from exc

        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
            reset_time = datetime.fromtimestamp(reset_at).isoformat() if reset_at else "soon"
            resource = response.headers.get("X-RateLimit-Resource", "api")
            token_hint = (
                f"Authenticated GitHub {resource} rate limit exceeded."
                if self.token
                else "GitHub API rate limit exceeded. Set a GITHUB_TOKEN to raise the limit."
            )
            raise GitHubRateLimitError(
                f"{token_hint} Resets at {reset_time}. "
                "GitHub search has a much smaller per-minute bucket than normal API calls."
            )
        return response

    def _friendly_http_error(self, exc: requests.exceptions.HTTPError) -> GitHubClientError:
        """Return a plain-English GitHubClientError for common HTTP failures."""
        response = exc.response
        if response is None:
            return GitHubClientError(
                "GitHub request failed after retries. The commit hash or repository URL may be invalid."
            )
        status = response.status_code
        url = response.url or ""
        endpoint_path = url.replace(self.BASE_URL, "")

        if status == 404:
            if "/commits/" in endpoint_path:
                return GitHubClientError(
                    "Commit not found. Make sure the commit hash is correct and belongs to this repository."
                )
            if "/repos/" in endpoint_path:
                return GitHubClientError(
                    "Repository not found. Check the GitHub URL and that you have access to it."
                )
            return GitHubClientError(
                "The requested resource was not found on GitHub. Check the repository URL and commit hash."
            )

        if status == 422:
            if "/commits/" in endpoint_path:
                return GitHubClientError(
                    "Invalid commit hash. Make sure you are using the full SHA and it exists in the repository."
                )
            return GitHubClientError(
                "GitHub could not process the request. Check the repository URL and commit hash are valid."
            )

        if status == 401:
            return GitHubClientError(
                "GitHub authentication failed. Check your GITHUB_TOKEN is valid."
            )

        if status == 403:
            return GitHubClientError(
                "GitHub refused the request. You may need a valid GITHUB_TOKEN or the resource is restricted."
            )

        if status >= 500:
            return GitHubClientError(
                "GitHub is having trouble right now. Try again in a moment."
            )

        return GitHubClientError(f"GitHub returned an error ({status}). Please try again.")

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        response = self._request(endpoint, params)
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise self._friendly_http_error(exc) from exc
        return response.json()

    def _paginate(self, endpoint: str, params: dict | None = None, max_pages: int | None = None) -> list:
        results: list = []
        params = params or {}
        params["per_page"] = 100
        url = f"{self.BASE_URL}{endpoint}"
        pages = max_pages or self.max_pages
        for _ in range(pages):
            response = self._request(url, params)
            if response.status_code == 404:
                break
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                raise self._friendly_http_error(exc) from exc
            data = response.json()
            if not isinstance(data, list):
                break
            results.extend(data)
            link = response.headers.get("Link", "")
            next_url = self._parse_next_link(link)
            if not next_url:
                break
            url = next_url
            params = {}
        return results

    @staticmethod
    def _parse_next_link(link_header: str) -> str | None:
        for part in link_header.split(","):
            url, rel = part.split(";") if ";" in part else (part, "")
            if 'rel="next"' in rel:
                return url.strip().strip("<>").strip()
        return None

    def get_repo(self, owner: str, repo: str) -> dict:
        data = self._get(f"/repos/{owner}/{repo}")
        if data is None:
            raise GitHubClientError(f"Repository {owner}/{repo} not found.")
        return data

    def get_repo_languages(self, owner: str, repo: str) -> dict[str, int]:
        """Fetch language line counts for a repository."""
        data = self._get(f"/repos/{owner}/{repo}/languages")
        return data or {}

    def get_repo_tree(self, owner: str, repo: str, sha: str | None = None) -> list[dict]:
        """Fetch the repository file tree (recursive) for structural analysis."""
        params: dict[str, Any] = {"recursive": "1"}
        if sha:
            endpoint = f"/repos/{owner}/{repo}/git/trees/{sha}"
        else:
            endpoint = f"/repos/{owner}/{repo}/git/trees/HEAD"
        data = self._get(endpoint, params)
        if not data:
            return []
        return data.get("tree", [])

    def get_commit(self, owner: str, repo: str, sha: str) -> dict | None:
        return self._get(f"/repos/{owner}/{repo}/commits/{sha}")

    def get_issues(self, owner: str, repo: str, state: str = "all") -> list:
        """Fetch issues (excludes pull requests by default)."""
        params = {"state": state, "sort": "updated", "direction": "desc"}
        return self._paginate(f"/repos/{owner}/{repo}/issues", params)

    def get_pull_requests(self, owner: str, repo: str, state: str = "all") -> list:
        params = {"state": state, "sort": "updated", "direction": "desc"}
        return self._paginate(f"/repos/{owner}/{repo}/pulls", params)

    def get_recent_merged_pull_requests(self, owner: str, repo: str, limit: int = 8) -> list:
        """Fetch recently updated PRs and keep merged ones for coupling checks."""
        prs = self.get_pull_requests(owner, repo, state="closed")
        merged = [pr for pr in prs if pr.get("merged_at")]
        return merged[:limit]

    def get_recent_workflow_runs(self, owner: str, repo: str, limit: int = 4) -> list:
        data = self._get(
            f"/repos/{owner}/{repo}/actions/runs",
            params={"per_page": limit, "status": "completed"},
        )
        return data.get("workflow_runs", []) if data else []

    def get_branches(self, owner: str, repo: str) -> list:
        return self._paginate(f"/repos/{owner}/{repo}/branches")

    def get_forks(self, owner: str, repo: str, sort: str = "stargazers") -> list:
        """Fetch repository forks, ordered so prominent community forks appear first."""
        params = {"sort": sort}
        return self._paginate(f"/repos/{owner}/{repo}/forks", params, max_pages=1)

    def search_issues_and_prs(
        self,
        query: str,
        owner: str,
        repo: str,
        item_type: str | None = None,
        per_page: int = 20,
    ) -> list:
        """Search old and current issues/PRs by keyword within a repository.

        The regular issues and PR endpoints are sorted by recent activity, which
        misses old merged pull requests. GitHub's search index lets us recover
        older public prior-art evidence such as long-merged implementation PRs.
        """
        clauses = [query, f"repo:{owner}/{repo}"]
        if item_type == "issue":
            clauses.append("is:issue")
        elif item_type == "pull_request":
            clauses.append("is:pr")
        params = {
            "q": " ".join(c for c in clauses if c),
            "sort": "updated",
            "order": "desc",
            "per_page": per_page,
        }
        data = self._get("/search/issues", params=params)
        return data.get("items", []) if data else []

    def search_code(self, query: str, owner: str, repo: str) -> list:
        """Search code within the repository."""
        q = f"{query} repo:{owner}/{repo}"
        data = self._get("/search/code", params={"q": q})
        return data.get("items", []) if data else []

    def search_commits(self, query: str, owner: str, repo: str) -> list:
        """Search commits within the repository."""
        q = f"{query} repo:{owner}/{repo}"
        data = self._get("/search/commits", params={"q": q})
        return data.get("items", []) if data else []

    def search_repositories(
        self,
        visibility: str | None = None,
        language: str | None = None,
        min_stars: int = 500,
        max_stars: int | None = None,
        pushed_after: str | None = None,
        license: str | None = None,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 100,
        max_pages: int = 3,
    ) -> tuple[str, list[dict]]:
        """Search public GitHub repositories matching the given criteria.

        GitHub's search does not support OR between qualifiers, so this helper
        builds a query for a single language and (optionally) a single license.
        Callers that need multiple languages or licenses should run this once
        per combination and merge the results themselves.

        When max_stars is provided, a GitHub range query (`stars:min..max`) is
        used so the search finds repositories in that band directly rather than
        returning only the most popular repositories and filtering them out.

        Returns a tuple of (query_string, repository_items).
        """
        clauses: list[str] = []
        if visibility:
            clauses.append(f"is:{visibility}")
        if language:
            clauses.append(f"language:{language}")
        if min_stars is not None and max_stars is not None:
            clauses.append(f"stars:{min_stars}..{max_stars}")
        elif min_stars is not None:
            clauses.append(f"stars:>={min_stars}")
        if pushed_after:
            clauses.append(f"pushed:>={pushed_after}")
        if license:
            slug = LICENSE_SLUG_MAP.get(license, license.lower().replace(" ", "-"))
            clauses.append(f"license:{slug}")

        query = " ".join(clauses)
        params = {"q": query, "sort": sort, "order": order, "per_page": per_page}
        url = f"{self.BASE_URL}/search/repositories"
        items: list[dict] = []
        for _ in range(max_pages):
            response = self._request(url, params)
            if response.status_code == 404:
                break
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                raise self._friendly_http_error(exc) from exc
            data = response.json()
            page_items = data.get("items", [])
            if not isinstance(page_items, list):
                break
            items.extend(page_items)
            link = response.headers.get("Link", "")
            next_url = self._parse_next_link(link)
            if not next_url:
                break
            url = next_url
            params = {}
        return query, items

    def search_related_repositories(
        self,
        repo_name: str,
        min_stars: int = 100,
        per_page: int = 10,
    ) -> list[dict]:
        """Find prominent repositories with the same name.

        Some community rewrites or successor projects are not represented as
        GitHub forks, but they can still be public prior art for a challenge.
        """
        query = f"{repo_name} in:name stars:>={min_stars}"
        data = self._get(
            "/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
        )
        return data.get("items", []) if data else []

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")

    def get_pull_request_commits(self, owner: str, repo: str, pr_number: int) -> list:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/commits")


LICENSE_SLUG_MAP = {
    "MIT": "mit",
    "BSD-1-Clause": "bsd-1-clause",
    "BSD-2-Clause": "bsd-2-clause",
    "BSD-2-Clause-Flex": "bsd-2-clause-flex",
    "BSD-2-Clause-FreeBSD": "bsd-2-clause-freebsd",
    "BSD-2-Clause-Modification": "bsd-2-clause-modification",
    "BSD-2-Clause-Patent": "bsd-2-clause-patent",
    "BSD-2-Clause-Views": "bsd-2-clause-views",
    "BSD-3-Clause": "bsd-3-clause",
    "BSD-3-Clause-Attribution": "bsd-3-clause-attribution",
    "BSD-3-Clause-EricHeitz": "bsd-3-clause-ericheitz",
    "BSD-3-Clause-HealthLevelSeven": "bsd-3-clause-healthlevelseven",
    "BSD-3-Clause-LBNL": "bsd-3-clause-lbnl",
    "BSD-3-Clause-Modification": "bsd-3-clause-modification",
    "BSD-3-Clause-OpenMPI": "bsd-3-clause-openmpi",
    "BSD-3-Clause-plus-CMU-Attribution": "bsd-3-clause-plus-cmu-attribution",
    "BSD-3-Clause-plus-Paul-Mackerras-Attribution": "bsd-3-clause-plus-paul-mackerras-attribution",
    "BSD-3-Clause-plus-Tommi-Komulainen-Attribution": "bsd-3-clause-plus-tommi-komulainen-attribution",
    "BSD-4-Clause": "bsd-4-clause",
    "BSD-4-Clause-Argonne": "bsd-4-clause-argonne",
    "BSD-4-Clause-Atmel": "bsd-4-clause-atmel",
    "BSD-4-Clause-Giffin": "bsd-4-clause-giffin",
    "BSD-4-Clause-PC-SC-Lite": "bsd-4-clause-pc-sc-lite",
    "BSD-4-Clause-Plus-Modification-Notice": "bsd-4-clause-plus-modification-notice",
    "BSD-4-Clause-UC": "bsd-4-clause-uc",
    "BSD-4-Clause-Visigoth": "bsd-4-clause-visigoth",
    "BSD-4-Clause-Vocal": "bsd-4-clause-vocal",
    "BSD-4-Clause-Wasabi": "bsd-4-clause-wasabi",
    "BSD-4.3TAHOE": "bsd-4.3tahoe",
    "BSD-5-Clause": "bsd-5-clause",
    "BSD-FatFs": "bsd-fatfs",
    "BSD-Mixed-2-Clause-And-3-Clause": "bsd-mixed-2-clause-and-3-clause",
    "BSD-Protection": "bsd-protection",
    "BSD-Source-Code": "bsd-source-code",
    "Boost": "bsl-1.0",
    "BSL-1.0": "bsl-1.0",
    "Apache": "apache-2.0",
    "Apache-2.0": "apache-2.0",
    "Apache-2.0-Modified": "apache-2.0-modified",
    "Apache-with-LLVM-Exception": "apache-2.0-with-llvm-exception",
    "Apache-with-Runtime-Exception": "apache-2.0-with-runtime-exception",
    "CC-BY-1.0": "cc-by-1.0",
    "CC-BY-2.0": "cc-by-2.0",
    "CC-BY-2.5": "cc-by-2.5",
    "CC-BY-3.0": "cc-by-3.0",
    "CC-BY-4.0": "cc-by-4.0",
    "GNU-All-permissive-Copying-License": "gnu-all-permissive-copying-license",
    "BLAS": "blas",
    "Other": "other",
}


def normalize_github_url(url: str) -> tuple[str, str]:
    return GitHubClient.parse_repo_url(url)
