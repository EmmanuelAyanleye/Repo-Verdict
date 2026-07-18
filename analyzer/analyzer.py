"""Core feature analysis logic for RepoVerdict."""
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .github_client import GitHubClient

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


REJECTION_LABELS = {
    "wontfix",
    "declined",
    "rejected",
    "not planned",
    "not_planned",
    "duplicate",
    "invalid",
}

REJECTION_KEYWORDS = [
    # Maintainer-decline phrases — require whole phrases to avoid matching technical text.
    "not something we would be adding",
    "not something we would add",
    "not something we are going to add",
    "dont think this is something we would be adding",
    "don't think this is something we would be adding",
    "dont think this is something we would add",
    "don't think this is something we would add",
    "not widely used",
    "no plans to add",
    "no plan to add",
    "no plan to support",
    "no plans to support",
    "will not implement",
    "won't implement",
    "will not be implementing",
    "won't be implementing",
    "not in scope",
    "out of scope",
    "not accepting",
    "not planned",
    "not going to implement",
    "do not plan",
    "don't plan",
    "we are not going to implement",
    "closing this as not planned",
    "declined to implement",
    "decided not to implement",
    "reject this proposal",
    "rejecting this proposal",
    "we have discussed this feature with the team",
    "we have discussed this and",
    "we wont be adding",
    "we won't be adding",
]

VERDICTS = ["existing", "open", "in_progress", "rejected", "closed", "new"]


@dataclass
class EvidenceItem:
    type: str  # issue, pull_request, commit, branch
    number: int | None
    title: str
    url: str
    state: str
    author: str
    body: str
    relevance: float
    reason: str
    matched_keywords: list[str] = field(default_factory=list)
    closed_at: str | None = None
    merged: bool = False
    merged_at: str | None = None
    labels: list[str] = field(default_factory=list)
    is_rejection: bool = False
    distinctive_matches: int = 0
    repository: str = ""
    repository_relation: str = ""


@dataclass
class AnalysisResult:
    verdict: str
    confidence: float
    summary: str
    evidence: dict[str, list[EvidenceItem]]
    details: list[str]
    feature_keywords: list[str] = field(default_factory=list)


class FeatureAnalyzer:
    def __init__(self):
        self.client = GitHubClient()
        self.llm_client = None
        if OpenAI and settings.OPENAI_API_KEY:
            self.llm_client = OpenAI(api_key=settings.OPENAI_API_KEY)

    @staticmethod
    def _stem(word: str) -> str:
        """Conservative English stemmer for cross-form matching."""
        word = word.lower().strip()
        if len(word) <= 3:
            return word
        suffixes = [
            "ationally", "fully", "fulness", "ively", "iveness", "ization",
            "ations", "ation", "ators", "ator", "ment", "ness", "ible", "ably",
            "ingly", "edly", "tion", "sion", "less", "ally", "ical", "ifies",
            "ify", "izes", "ize", "ises", "ise", "ings", "ing", "ed", "er",
            "est", "ly", "al", "y", "s",
        ]
        for suffix in suffixes:
            if word.endswith(suffix) and len(word) - len(suffix) > 2:
                return word[: -len(suffix)]
        return word

    @classmethod
    def _normalize_text(cls, text: str) -> str:
        """Normalize text for matching: lowercase, strip punctuation, stem words."""
        text = re.sub(r"[^\w\s]", " ", text)
        words = [cls._stem(w) for w in text.split() if len(w) > 2]
        return " " + " ".join(words) + " "

    def analyze(
        self,
        repo_url: str,
        commit_hash: str,
        feature: dict[str, Any],
    ) -> AnalysisResult:
        owner, repo = self.client.parse_repo_url(repo_url)

        # Build keyword set from the feature description
        feature_text = self._build_feature_text(feature)
        self._current_feature = feature
        keywords, tech_terms = self._extract_keywords(
            feature_text, feature.get("title", "")
        )
        self._tech_terms = tech_terms

        # Fetch repository data
        repo_data = self.client.get_repo(owner, repo)
        default_branch = repo_data.get("default_branch", "main")

        commit = self.client.get_commit(owner, repo, commit_hash)
        issues = self.client.get_issues(owner, repo, state="all")
        pull_requests = self.client.get_pull_requests(owner, repo, state="all")
        branches = self.client.get_branches(owner, repo)

        # Filter out pull requests disguised as issues
        issues = [i for i in issues if "pull_request" not in i]

        # Compute inverse-document-frequency weights for keywords based on the fetched
        # issue/PR corpus. Generic words like "memory" or "token" in an agent repo appear
        # everywhere, so they get low weight; rare technical terms like "compaction" get
        # high weight.
        self._keyword_weights = self._compute_keyword_weights(
            keywords, issues + pull_requests
        )

        # Search can find older matching issues/PRs that pagination by recent
        # activity misses, especially long-merged implementation PRs.
        searched_issues, searched_prs = self._search_keyword_evidence(
            owner, repo, keywords, repository=f"{owner}/{repo}"
        )
        issues.extend(searched_issues)
        pull_requests.extend(searched_prs)

        # Prominent forks and parent/source repos can contain public prior art
        # even when the exact target repository does not. This is intentionally
        # bounded to keep API usage predictable.
        related_issues, related_prs = self._related_repository_evidence(
            owner, repo, repo_data, keywords
        )
        issues.extend(related_issues)
        pull_requests.extend(related_prs)

        # Score relevance
        scored_issues = [self._score_item(i, keywords, "issue") for i in issues]
        scored_prs = [self._score_item(pr, keywords, "pull_request") for pr in pull_requests]

        # Commits: search by keywords plus fetch commit at hash
        commit_items: list[EvidenceItem] = []
        if commit:
            commit_items.append(self._commit_to_evidence(commit, keywords, is_base=True))

        for keyword in keywords[:5]:
            try:
                found_commits = self.client.search_commits(keyword, owner, repo)
                for c in found_commits[:5]:
                    commit_items.append(self._commit_to_evidence(c, keywords))
            except Exception:
                pass

        # Branches
        scored_branches = [self._score_branch(b, keywords, owner, repo) for b in branches]

        # Sort by relevance and deduplicate
        scored_issues = self._deduplicate(sorted(scored_issues, key=lambda x: x.relevance, reverse=True))
        scored_prs = self._deduplicate(sorted(scored_prs, key=lambda x: x.relevance, reverse=True))
        commit_items = self._deduplicate(sorted(commit_items, key=lambda x: x.relevance, reverse=True))
        scored_branches = self._deduplicate(sorted(scored_branches, key=lambda x: x.relevance, reverse=True))

        # Determine verdict and confidence. The function also returns the evidence
        # items it cited so we can guarantee they appear in the UI.
        verdict, confidence, summary, details, cited = self._determine_verdict(
            feature, scored_issues, scored_prs, commit_items, scored_branches
        )

        # Optional LLM refinement (keeps the same cited items from keyword analysis)
        if self.llm_client:
            verdict, confidence, summary, details, cited = self._llm_enhance(
                feature, verdict, confidence, summary, details, cited,
                scored_issues, scored_prs, commit_items, scored_branches
            )

        # Make sure the evidence that drove the verdict is returned to the frontend
        # even if its general relevance score is below the top-10 cutoff. Otherwise
        # the UI can cite an issue/PR the user cannot find.
        feature_words = set(self._normalize_text(self._build_feature_text(feature)).split())
        rejected_issues = [
            i for i in scored_issues
            if i.is_rejection and i.relevance > 0.10 and self._item_matches_topic(i, feature_words)
        ]
        rejected_prs = [
            p for p in scored_prs
            if p.is_rejection and p.relevance > 0.10 and self._item_matches_topic(p, feature_words)
        ]
        cited_issues = [i for i in cited if i.type == "issue"]
        cited_prs = [p for p in cited if p.type == "pull_request"]
        cited_commits = [c for c in cited if c.type == "commit"]
        cited_branches = [b for b in cited if b.type == "branch"]
        issues_evidence = self._deduplicate(cited_issues + rejected_issues + scored_issues[:10])
        prs_evidence = self._deduplicate(cited_prs + rejected_prs + scored_prs[:10])
        commits_evidence = self._deduplicate(cited_commits + commit_items[:10])
        branches_evidence = self._deduplicate(cited_branches + scored_branches[:10])

        return AnalysisResult(
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            evidence={
                "issues": issues_evidence,
                "pull_requests": prs_evidence,
                "commits": commits_evidence,
                "branches": branches_evidence,
            },
            details=details,
            feature_keywords=keywords,
        )

    def _search_terms(self, feature: dict[str, Any], keywords: list[str]) -> list[str]:
        """Pick a small set of high-signal terms for GitHub issue/PR search."""
        title = feature.get("title", "")
        terms: list[str] = []

        title_words = [
            w for w in re.sub(r"[^\w\s]", " ", title.lower()).split()
            if len(w) > 4 and w not in self.GENERIC_TERMS
        ]
        for n in (3, 2):
            for i in range(len(title_words) - n + 1):
                terms.append(" ".join(title_words[i:i + n]))

        tech_terms = sorted(getattr(self, "_tech_terms", set()), key=len, reverse=True)
        terms.extend(tech_terms)
        terms.extend(k for k in keywords if len(k) > 4 and k not in self.GENERIC_TERMS)

        seen: set[str] = set()
        out: list[str] = []
        for term in terms:
            clean = term.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            out.append(f'"{clean}"' if " " in clean else clean)
            if len(out) >= getattr(settings, "RELATED_SEARCH_TERMS", 8):
                break
        return out

    def _search_keyword_evidence(
        self,
        owner: str,
        repo: str,
        keywords: list[str],
        repository: str,
        relation: str = "target",
    ) -> tuple[list[dict], list[dict]]:
        """Search GitHub issues/PRs for distinctive feature terms."""
        feature = getattr(self, "_current_feature", {})
        issues: list[dict] = []
        prs: list[dict] = []
        for term in self._search_terms(feature, keywords):
            try:
                issue_results = self.client.search_issues_and_prs(
                    term, owner, repo, item_type="issue", per_page=10
                )
                pr_results = self.client.search_issues_and_prs(
                    term, owner, repo, item_type="pull_request", per_page=10
                )
            except Exception:
                continue
            for item in issue_results:
                item["_repository"] = repository
                item["_repository_relation"] = relation
                issues.append(item)
            for item in pr_results:
                item["_repository"] = repository
                item["_repository_relation"] = relation
                prs.append(item)
        return issues, prs

    def _related_repository_evidence(
        self,
        owner: str,
        repo: str,
        repo_data: dict,
        keywords: list[str],
    ) -> tuple[list[dict], list[dict]]:
        """Collect issue/PR evidence from parent/source repos and prominent forks."""
        if not getattr(settings, "ANALYZE_RELATED_REPOS", True):
            return [], []

        max_related = getattr(settings, "RELATED_REPO_LIMIT", 4)
        min_stars = getattr(settings, "RELATED_FORK_MIN_STARS", 100)
        related: list[tuple[str, str, str]] = []

        for key, relation in (("parent", "parent"), ("source", "source")):
            related_repo = repo_data.get(key) or {}
            full_name = related_repo.get("full_name", "")
            if full_name and full_name.lower() != f"{owner}/{repo}".lower():
                r_owner, r_repo = full_name.split("/", 1)
                related.append((r_owner, r_repo, relation))

        if len(related) < max_related:
            try:
                same_name_repos = self.client.search_related_repositories(
                    repo, min_stars=min_stars, per_page=10
                )
            except Exception:
                same_name_repos = []

            for candidate in same_name_repos:
                full_name = candidate.get("full_name", "")
                name = candidate.get("name", "")
                if not full_name or full_name.lower() == f"{owner}/{repo}".lower():
                    continue
                if name.lower() != repo.lower():
                    continue
                c_owner, c_repo = full_name.split("/", 1)
                related.append((c_owner, c_repo, "same-name"))
                if len(related) >= max_related:
                    break

        if len(related) < max_related:
            try:
                forks = self.client.get_forks(owner, repo)
            except Exception:
                forks = []

            for fork in forks:
                full_name = fork.get("full_name", "")
                if not full_name or full_name.lower() == f"{owner}/{repo}".lower():
                    continue
                if fork.get("stargazers_count", 0) < min_stars:
                    continue
                f_owner, f_repo = full_name.split("/", 1)
                related.append((f_owner, f_repo, "fork"))
                if len(related) >= max_related:
                    break

        issues: list[dict] = []
        prs: list[dict] = []
        seen_repos: set[str] = set()
        for r_owner, r_repo, relation in related[:max_related]:
            full_name = f"{r_owner}/{r_repo}"
            if full_name.lower() in seen_repos:
                continue
            seen_repos.add(full_name.lower())
            found_issues, found_prs = self._search_keyword_evidence(
                r_owner, r_repo, keywords, repository=full_name, relation=relation
            )
            issues.extend(found_issues)
            prs.extend(found_prs)
        return issues, prs

    @staticmethod
    def _build_feature_text(feature: dict[str, Any]) -> str:
        parts = [
            feature.get("title", ""),
            feature.get("category", ""),
            feature.get("description", ""),
            feature.get("technical_scope", ""),
            feature.get("exploration_angle", ""),
        ]
        return "\n".join(str(p) for p in parts if p)

    # Terms that appear in almost every GitHub issue/PR and should not drive relevance.
    GENERIC_TERMS: set[str] = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
        "how", "its", "may", "new", "now", "old", "see", "two", "way", "who",
        "boy", "did", "she", "use", "her", "now", "him", "than", "like", "time",
        "will", "with", "from", "they", "know", "want", "been", "good", "much",
        "some", "come", "could", "would", "there", "their", "what", "said", "each",
        "which", "this", "that", "have", "more", "very", "after", "also", "back",
        "other", "many", "than", "then", "them", "these", "could", "should", "into",
        "such", "when", "where", "while", "about", "before", "being", "between",
        "both", "through", "during", "without", "against", "above", "below", "over",
        "under", "again", "further", "once", "here", "why", "how", "all", "any",
        "both", "each", "few", "more", "most", "other", "some", "such", "only",
        "own", "same", "so", "than", "too", "very", "can", "will", "just", "should",
        "add", "new", "feature", "request", "bug", "fix", "refactor", "optimization",
        "introduces", "implements", "support", "adds", "issue", "enhancement",
        "repo", "repository", "code", "python", "file", "files", "module", "modules",
        "class", "function", "method", "change", "changes", "update", "updates",
        "improve", "improvement", "implementation", "implement", "use", "using",
        "current", "currently", "existing", "project", "application", "app",
        "need", "needs", "want", "should", "would", "could", "please", "thanks",
        "help", "question", "problem", "error", "issue", "pr", "pull", "request",
    }

    def _extract_technical_terms(self, text: str) -> set[str]:
        """Extract distinctive technical identifiers from the feature text.\n\nIncludes backticked code, CamelCase/PascalCase identifiers, snake_case names,
file paths, and hyphenated technical terms. These are weighted heavily during
scoring because they are far more distinctive than generic English words.

We keep the identifiers intact rather than splitting them into their parts, so
that `TokenLimitExceeded` stays one distinctive signal instead of polluting the
keyword set with generic words like `limit` and `exceeded`.
        """
        def _looks_like_example(term: str) -> bool:
            """Skip terms that are example snippets or placeholders rather than
            real code identifiers (e.g. `color: {}`, `bold: "yes"`, `123`, `\r\n`)."""
            if any(c in term for c in ['"', "'", "\\", "->", ":", "{", "}", "[", "]", "#"]):
                return True
            if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", term):
                return True
            return False

        terms: set[str] = set()

        # Backticked code terms — keep the whole raw term only. Individual word
        # parts are intentionally not added here; the snake_case and CamelCase
        # regexes below already pick out real identifiers, and adding every part
        # of a backticked signature (e.g. "Console" from "Console.print_json")
        # turns common class names into false "tech terms" that dominate scoring.
        for raw in re.findall(r"`([^`]+)`", text):
            full = raw.lower().strip()
            if len(full) > 2 and not _looks_like_example(full):
                terms.add(full)

        # CamelCase / PascalCase identifiers (e.g. TokenLimitExceeded, MemoryCompactor)
        for raw in re.findall(r"\b[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]+)+\b", text):
            full = raw.lower().strip()
            if len(full) > 2 and not _looks_like_example(full):
                terms.add(full)

        # snake_case identifiers and file paths (e.g. app/memory/compaction.py)
        for raw in re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|(?:\w+/)+\w+(?:\.\w+)?", text):
            full = raw.lower().strip()
            if len(full) > 2 and not _looks_like_example(full):
                terms.add(full)
            # Only add non-generic path/underscore parts if they look distinctive.
            for part in re.split(r"[_/]+", raw):
                part = part.lower().strip()
                if (
                    len(part) > 4
                    and part not in self.GENERIC_TERMS
                    and not part.isdigit()
                    and not _looks_like_example(part)
                ):
                    terms.add(part)

        # Hyphenated technical terms (e.g. sliding-window) — keep the whole term.
        for raw in re.findall(r"\b[a-zA-Z]+-[a-zA-Z-]+\b", text):
            term = raw.lower().strip()
            if not _looks_like_example(term):
                terms.add(term)

        return terms

    def _extract_keywords(self, text: str, title: str = "") -> tuple[list[str], set[str]]:
        """Extract a distinctive keyword set from the feature text.\n\nReturns (keywords, tech_terms) where tech_terms are distinctive technical
identifiers (code terms, CamelCase names, file paths, etc.) that should be
weighted heavily during scoring.
        """
        # Pull technical terms first — these are the strongest signals.
        tech_terms = self._extract_technical_terms(text)

        # Strip code blocks and URLs, but keep inline code in the running text for frequency.
        text_for_freq = re.sub(r"```[\s\S]*?```", " ", text)
        text_for_freq = re.sub(r"https?://\S+", " ", text_for_freq)
        text_for_freq = re.sub(r"[^\w\s]", " ", text_for_freq)

        words = [w.lower() for w in text_for_freq.split() if len(w) > 2]
        words = [w for w in words if w not in self.GENERIC_TERMS]

        # Count frequency of distinctive single-word terms
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        single_terms = [w for w, _ in ranked[:20] if w not in self.GENERIC_TERMS]

        # Build title word set and phrases (bigrams + trigrams)
        title_clean = re.sub(r"[^\w\s]", " ", title.lower())
        title_words = [w for w in title_clean.split() if len(w) > 2 and w not in self.GENERIC_TERMS]
        title_word_set = list(dict.fromkeys(title_words))
        phrases: list[str] = []
        for n in (3, 2):
            for i in range(len(title_words) - n + 1):
                phrase = " ".join(title_words[i : i + n])
                if phrase:
                    phrases.append(phrase)

        # Combine: title words, technical terms, title phrases, body/scope terms
        seen: set[str] = set()
        keywords: list[str] = []
        for kw in title_word_set + sorted(tech_terms) + [p for p in phrases if p] + single_terms:
            if kw not in seen:
                seen.add(kw)
                keywords.append(kw)
        return keywords[:30], tech_terms

    def _compute_keyword_weights(
        self, keywords: list[str], items: list[dict]
    ) -> dict[str, float]:
        """Compute inverse-document-frequency weights for each keyword.\n\nRare terms that appear in few issues/PRs get higher weight than generic terms\nthat appear everywhere (e.g. "memory", "token" in an agent repository).
        """
        if not items:
            return {kw: 1.0 for kw in keywords}

        total = len(items)
        weights: dict[str, float] = {}
        for kw in keywords:
            norm_kw = self._normalize_text(kw).strip()
            if not norm_kw:
                weights[kw] = 1.0
                continue
            # A document matches if the normalized keyword appears anywhere in its text
            doc_count = 0
            for it in items:
                text = f"{it.get('title', '')}\n{it.get('body') or ''}".lower()
                norm_text = self._normalize_text(text)
                if norm_kw in norm_text:
                    doc_count += 1
            # Aggressive IDF: common terms are heavily discounted, rare technical terms
            # get a strong boost. Cap at 10 so extreme outliers do not dominate.
            idf = min(10.0, (total + 1) / (doc_count + 1))
            weights[kw] = idf
        return weights

    def _score_item(self, item: dict, keywords: list[str], item_type: str) -> EvidenceItem:
        title = item.get("title", "")
        body = item.get("body") or ""
        labels = [label.get("name", "").lower() for label in item.get("labels", [])]
        text = f"{title}\n{body}\n{' '.join(labels)}".lower()

        norm_text = self._normalize_text(text)
        norm_title = self._normalize_text(title)
        tech_terms = getattr(self, "_tech_terms", set())
        keyword_weights = getattr(self, "_keyword_weights", {})

        matched: list[str] = []
        score = 0.0
        phrase_matches = 0
        title_phrase_matches = 0
        tech_matches = 0
        distinctive_matches = 0

        for kw in keywords:
            norm_kw = self._normalize_text(kw).strip()
            if not norm_kw or norm_kw not in norm_text:
                continue
            matched.append(kw)
            idf = keyword_weights.get(kw, 1.0)
            is_phrase = " " in kw.strip()
            is_tech = kw in tech_terms
            is_distinctive = is_phrase or is_tech or idf > 2.0
            if is_phrase:
                phrase_matches += 1
                if norm_kw in norm_title:
                    title_phrase_matches += 1
                    w = 10.0 * idf
                else:
                    w = 2.0 * idf
            elif is_tech:
                # Technical identifiers are strong signals
                tech_matches += 1
                if norm_kw in norm_title:
                    w = 8.0 * idf
                else:
                    w = 2.0 * idf
            else:
                if norm_kw in norm_title:
                    w = 4.0 * idf
                else:
                    w = 0.8 * idf
            score += w
            if is_distinctive:
                distinctive_matches += 1

        # Normalize against the weighted keyword space
        max_possible = 0.0
        for kw in keywords:
            idf = keyword_weights.get(kw, 1.0)
            if " " in kw.strip():
                max_possible += 10.0 * idf
            elif kw in tech_terms:
                max_possible += 8.0 * idf
            else:
                max_possible += 4.0 * idf
        max_possible = max_possible or 1.0

        coverage = len(matched) / max(1, len(keywords))
        base_relevance = score / max_possible
        # Scale up so meaningful keyword overlap reaches the verdict threshold
        relevance = min(0.99, (base_relevance * 0.6 + coverage * 0.4) * 1.5)

        # Cap relevance when there are no distinctive matches (phrases, technical
        # identifiers, or high-IDF terms). Generic word overlap alone is not enough
        # to claim a strong relationship.
        if distinctive_matches == 0 and relevance > 0.25:
            relevance = min(relevance, 0.25)

        state = item.get("state", "unknown")
        number = item.get("number")
        url = item.get("html_url", "")
        author = item.get("user", {}).get("login", "")
        closed_at = item.get("closed_at")

        merged = False
        merged_at = None
        if item_type == "pull_request":
            pr_info = item.get("pull_request") or {}
            merged_at = item.get("merged_at") or pr_info.get("merged_at")
            merged = bool(merged_at)
            if merged:
                state = "merged"

        is_rejection = self._detect_rejection(item)
        reason = self._build_reason(item_type, state, matched, is_rejection)
        repository = item.get("_repository", "")
        repository_relation = item.get("_repository_relation", "")
        if repository and repository_relation and repository_relation != "target":
            reason = f"{reason} Found in {repository_relation} repository {repository}."

        return EvidenceItem(
            type=item_type,
            number=number,
            title=title,
            url=url,
            state=state,
            author=author,
            body=body[:2000],
            relevance=relevance,
            reason=reason,
            matched_keywords=matched[:10],
            closed_at=closed_at,
            merged=merged,
            merged_at=merged_at,
            labels=[label.get("name", "") for label in item.get("labels", [])],
            is_rejection=is_rejection,
            distinctive_matches=distinctive_matches,
            repository=repository,
            repository_relation=repository_relation,
        )

    def _commit_to_evidence(self, commit: dict, keywords: list[str], is_base: bool = False) -> EvidenceItem:
        message = commit.get("commit", {}).get("message", "")
        url = commit.get("html_url", "")
        author = (
            commit.get("author", {}) or {}
        ).get("login") or (
            commit.get("commit", {}).get("author", {}) or {}
        ).get("name", "")

        code_terms = getattr(self, "_tech_terms", set())
        keyword_weights = getattr(self, "_keyword_weights", {})
        norm_message = self._normalize_text(message)
        norm_first_line = self._normalize_text(message.split("\n")[0])
        matched = [kw for kw in keywords if self._normalize_text(kw).strip() in norm_message]
        score = 0.0
        for kw in matched:
            norm_kw = self._normalize_text(kw).strip()
            idf = keyword_weights.get(kw, 1.0)
            if " " in kw.strip():
                weight = 3.0 * idf
            elif kw in code_terms:
                weight = 2.5 * idf
            else:
                weight = 1.0 * idf
            if norm_kw in norm_first_line:
                score += weight * 2.0
            else:
                score += weight

        max_possible = sum(
            (
                3.0 if " " in kw.strip() else (2.5 if kw in code_terms else 1.0)
            )
            * keyword_weights.get(kw, 1.0)
            for kw in keywords
        ) or 1.0
        coverage = len(matched) / max(1, len(keywords))
        relevance = min(0.99, (score / max_possible * 0.6 + coverage * 0.4) * 1.5)
        if is_base and not matched:
            relevance = 0.0

        return EvidenceItem(
            type="commit",
            number=None,
            title=message.split("\n")[0][:120],
            url=url,
            state="committed",
            author=author,
            body=message[:2000],
            relevance=relevance,
            reason=f"Commit matches keywords: {', '.join(matched[:5])}" if matched else "Commit at pinned base hash.",
            matched_keywords=matched[:10],
        )

    def _score_branch(self, branch: dict, keywords: list[str], owner: str, repo: str) -> EvidenceItem:
        name = branch.get("name", "")
        code_terms = getattr(self, "_tech_terms", set())
        keyword_weights = getattr(self, "_keyword_weights", {})
        norm_name = self._normalize_text(name)
        matched = [kw for kw in keywords if self._normalize_text(kw).strip() in norm_name]
        score = 0.0
        for kw in matched:
            idf = keyword_weights.get(kw, 1.0)
            if " " in kw.strip():
                score += 3.0 * idf
            elif kw in code_terms:
                score += 2.5 * idf
            else:
                score += 1.0 * idf
        max_possible = sum(
            (
                3.0 if " " in kw.strip() else (2.5 if kw in code_terms else 1.0)
            )
            * keyword_weights.get(kw, 1.0)
            for kw in keywords
        ) or 1.0
        coverage = len(matched) / max(1, len(keywords))
        relevance = min(0.95, (score / max_possible * 0.6 + coverage * 0.4) * 1.5)
        url = f"https://github.com/{owner}/{repo}/tree/{name}"
        return EvidenceItem(
            type="branch",
            number=None,
            title=name,
            url=url,
            state="active",
            author="",
            body="",
            relevance=relevance,
            reason=f"Branch name matches keywords: {', '.join(matched[:5])}" if matched else "",
            matched_keywords=matched[:10],
        )

    def _detect_rejection(self, item: dict) -> bool:
        labels = [label.get("name", "").lower() for label in item.get("labels", [])]
        if any(label in REJECTION_LABELS for label in labels):
            return True
        body = (item.get("body") or "").lower()
        title = item.get("title", "").lower()
        # Normalize contractions so "don't" matches "dont" patterns.
        contraction_map = {
            "don't": "dont",
            "won't": "wont",
            "can't": "cant",
            "wouldn't": "wouldnt",
            "shouldn't": "shouldnt",
            "couldn't": "couldnt",
            "isn't": "isnt",
            "aren't": "arent",
        }
        combined = f"{title} {body}"
        for full, short in contraction_map.items():
            combined = combined.replace(full, short)
        return any(kw in combined for kw in REJECTION_KEYWORDS)

    @staticmethod
    def _build_reason(item_type: str, state: str, matched: list[str], is_rejection: bool) -> str:
        type_label = "PR" if item_type == "pull_request" else item_type.capitalize()
        status = f"{state}"
        if is_rejection:
            status += " (rejection detected)"
        keyword_part = f"Matched keywords: {', '.join(matched[:5])}" if matched else "No strong keyword match"
        return f"{type_label} is {status}. {keyword_part}."

    @staticmethod
    def _deduplicate(items: list[EvidenceItem]) -> list[EvidenceItem]:
        seen: set[str] = set()
        out: list[EvidenceItem] = []
        for item in items:
            key = f"{item.repository}:{item.type}:{item.number}:{item.title}"
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def _item_matches_topic(self, item: EvidenceItem, title_words: set[str]) -> bool:
        """Check if any matched keyword of an item overlaps with the feature title
        AND that keyword is distinctive enough to be topically meaningful.
        The keyword must also appear as a whole word in the item's own title so
        that random body-only keyword matches (e.g. a stack trace mentioning
        'recordings') and substring matches (e.g. 'proxy' inside 'ProxyFix') do
        not falsely claim topical relevance.
        """
        keyword_weights = getattr(self, "_keyword_weights", {})
        tech_terms = getattr(self, "_tech_terms", set())
        norm_item_title = self._normalize_text(item.title)

        def _title_has_word(word: str) -> bool:
            # _normalize_text returns a string with leading/trailing spaces.
            return word and f" {word} " in norm_item_title

        for kw in item.matched_keywords:
            is_phrase = " " in kw.strip()
            is_tech = kw in tech_terms
            idf = keyword_weights.get(kw, 1.0)
            is_distinctive = is_phrase or is_tech or idf > 3.5

            norm_kw = self._normalize_text(kw).strip()
            if (
                norm_kw
                and norm_kw in title_words
                and _title_has_word(norm_kw)
                and is_distinctive
            ):
                return True
            # Also accept distinctive phrase subwords that appear in both titles.
            for part in kw.lower().split():
                part_norm = self._normalize_text(part).strip()
                part_idf = keyword_weights.get(part, 1.0)
                if (
                    part_norm
                    and part_norm in title_words
                    and _title_has_word(part_norm)
                    and part_idf > 3.5
                ):
                    return True
        return False

    def _determine_verdict(
        self,
        feature: dict,
        issues: list[EvidenceItem],
        prs: list[EvidenceItem],
        commits: list[EvidenceItem],
        branches: list[EvidenceItem],
    ) -> tuple[str, float, str, list[str], list[EvidenceItem]]:
        title = feature.get("title", "the requested feature")

        # Threshold for non-rejection verdicts. Keep it moderate: keyword-only matching
        # rarely produces extremely high scores, and a cluster of relevant technical terms
        # (e.g. fold/imaginary/DST) is a strong signal even at ~0.25.
        top_issues = [i for i in issues if i.relevance > 0.25]
        top_prs = [p for p in prs if p.relevance > 0.25]
        top_commits = [c for c in commits if c.relevance > 0.25]
        top_branches = [b for b in branches if b.relevance > 0.25]

        # Rejection evidence must contain explicit maintainer-decline language AND be
        # topically related to the feature. We require at least one matched keyword to
        # appear in the feature title so unrelated PRs that happen to contain decline
        # phrases (e.g. #22984 about reconstruct_patches) are not cited as rejections.
        feature_words = set(self._normalize_text(self._build_feature_text(feature)).split())
        rejected_issues = [
            i for i in issues
            if i.is_rejection and i.relevance > 0.10 and self._item_matches_topic(i, feature_words)
        ]
        rejected_prs = [
            p for p in prs
            if p.is_rejection and p.relevance > 0.10 and self._item_matches_topic(p, feature_words)
        ]

        details: list[str] = []

        # 1. Rejection takes priority
        if rejected_issues or rejected_prs:
            evidence = rejected_issues[0] if rejected_issues else rejected_prs[0]
            summary = (
                f"{title} appears to have been explicitly declined by the maintainers. "
                f"See {evidence.type} #{evidence.number}."
            )
            details.append(
                f"Rejection signal found in {evidence.type} #{evidence.number}: '{evidence.title}'."
            )
            return "rejected", 0.85, summary, details, [evidence]

        # 2. Existing / merged PRs
        # A merged PR only counts as "existing" if it is strongly relevant AND
        # topically aligned with the feature title. This prevents tangential PRs
        # (e.g. "Update shift() for issue #1145") from falsely claiming a feature
        # already exists when active open PRs are the real state of the work.
        merged_prs = [
            p for p in top_prs
            if p.state == "merged" and p.relevance > 0.35 and self._item_matches_topic(p, feature_words)
        ]
        open_prs = [p for p in top_prs if p.state == "open"]
        if merged_prs:
            best_merged = merged_prs[0]
            # If an open PR is comparably or more relevant, active development wins.
            if open_prs and open_prs[0].relevance >= best_merged.relevance * 0.95:
                pr = open_prs[0]
                repo_note = f" in {pr.repository}" if pr.repository else ""
                summary = f"{title} is currently being implemented in open PR #{pr.number}{repo_note}."
                details.append(f"Open PR #{pr.number}{repo_note}: '{pr.title}'")
                return "in_progress", 0.82, summary, details, [pr]
            repo_note = f" in {best_merged.repository}" if best_merged.repository else ""
            relation_note = (
                f" a related {best_merged.repository_relation} repository"
                if best_merged.repository_relation and best_merged.repository_relation != "target"
                else " the repository"
            )
            summary = (
                f"{title} or a very similar implementation already exists in{relation_note}. "
                f"It was merged via PR #{best_merged.number}{repo_note}."
            )
            details.append(f"Merged PR #{best_merged.number}{repo_note}: '{best_merged.title}'")
            if top_commits:
                details.append(f"Related commit: '{top_commits[0].title}'")
            return "existing", 0.88, summary, details, [best_merged]

        # 3. Existing in code / branches
        # A commit or branch must be topically about the feature title, not just
        # contain generic overlapping terms (e.g. a JSON color commit matching a
        # "console recording JSON export" feature). Require a distinctive keyword
        # overlap with the title to avoid false positives.
        code_matches = [
            c for c in top_commits
            if c.relevance > 0.35 and self._item_matches_topic(c, feature_words)
        ]
        branch_matches = [
            b for b in top_branches
            if b.relevance > 0.35 and self._item_matches_topic(b, feature_words)
        ]
        if code_matches or branch_matches:
            branch_names = [b.title for b in branch_matches[:3]]
            commit_titles = [c.title for c in code_matches[:3]]
            summary = f"{title} appears to already exist or be under active development in the repository."
            if branch_names:
                details.append(f"Related branches: {', '.join(branch_names)}")
            if commit_titles:
                details.append(f"Related commits: {', '.join(commit_titles)}")
            return "existing", 0.75, summary, details, (code_matches[:3] + branch_matches[:3])

        # 4. In progress (open PR)
        open_prs = [
            p for p in top_prs
            if p.state == "open" and self._item_matches_topic(p, feature_words)
        ]
        if open_prs:
            pr = open_prs[0]
            repo_note = f" in {pr.repository}" if pr.repository else ""
            summary = f"{title} is currently being implemented in open PR #{pr.number}{repo_note}."
            details.append(f"Open PR #{pr.number}{repo_note}: '{pr.title}'")
            return "in_progress", 0.82, summary, details, [pr]

        # 5. Open issue
        open_issues = [
            i for i in top_issues
            if i.state == "open" and self._item_matches_topic(i, feature_words)
        ]
        if open_issues:
            issue = open_issues[0]
            summary = f"{title} has an open issue (#{issue.number}) tracking the same request."
            details.append(f"Open issue #{issue.number}: '{issue.title}'")
            return "open", 0.78, summary, details, [issue]

        # 6. Closed issue/PR without merge or explicit rejection
        closed_items = [
            i for i in top_issues + top_prs
            if i.state in ("closed", "merged") and self._item_matches_topic(i, feature_words)
        ]
        if closed_items:
            item = closed_items[0]
            summary = f"{title} has related work that was previously closed."
            details.append(f"Closed {item.type} #{item.number}: '{item.title}'")
            return "closed", 0.65, summary, details, [item]

        # 7. New
        summary = f"No existing issue, PR, commit, or branch strongly matches {title}. It looks like genuinely new work."
        details.append("No strong matches found across issues, pull requests, commits, or branches.")
        return "new", 0.55, summary, details, []

    def _llm_enhance(
        self,
        feature: dict,
        verdict: str,
        confidence: float,
        summary: str,
        details: list[str],
        cited: list[EvidenceItem],
        issues: list[EvidenceItem],
        prs: list[EvidenceItem],
        commits: list[EvidenceItem],
        branches: list[EvidenceItem],
    ) -> tuple[str, float, str, list[str], list[EvidenceItem]]:
        """Use an LLM to refine the verdict and summary based on fetched evidence."""
        if not self.llm_client:
            return verdict, confidence, summary, details, cited

        evidence_text = self._evidence_to_text(issues, prs, commits, branches)
        feature_text = self._build_feature_text(feature)

        prompt = f"""You are RepoVerdict, an expert open-source contribution analyst.
A developer wants to contribute the following feature to a GitHub repository.

--- FEATURE DESCRIPTION ---
{feature_text}

--- REPOSITORY EVIDENCE ---
{evidence_text}

Based ONLY on the evidence above, classify the feature into EXACTLY ONE verdict:
new, existing, open, in_progress, closed, or rejected.

Return a JSON object with these keys:
- verdict: one of new/existing/open/in_progress/closed/rejected
- confidence: float between 0 and 1
- summary: one concise paragraph explaining the verdict
- details: list of 2-5 bullet strings citing specific issue/PR/commit numbers and URLs

Do not invent evidence. If the evidence is weak, return verdict "new" with lower confidence.
"""
        try:
            response = self.llm_client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1200,
            )
            content = response.choices[0].message.content or ""
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            result = json.loads(content)
            verdict = result.get("verdict", verdict).lower()
            confidence = float(result.get("confidence", confidence))
            summary = result.get("summary", summary)
            details = result.get("details", details)
        except Exception:
            # If LLM fails, keep keyword-based result
            pass
        return verdict, confidence, summary, details, cited

    @staticmethod
    def _evidence_to_text(
        issues: list[EvidenceItem],
        prs: list[EvidenceItem],
        commits: list[EvidenceItem],
        branches: list[EvidenceItem],
    ) -> str:
        lines: list[str] = []
        for i in issues[:8]:
            lines.append(
                f"Issue #{i.number} ({i.state}): {i.title}\nURL: {i.url}\nLabels: {', '.join(i.labels)}\n"
            )
        for p in prs[:8]:
            lines.append(
                f"PR #{p.number} ({p.state}): {p.title}\nURL: {p.url}\nLabels: {', '.join(p.labels)}\n"
            )
        for c in commits[:5]:
            lines.append(f"Commit {c.title}\nURL: {c.url}\n")
        for b in branches[:5]:
            lines.append(f"Branch: {b.title}\nURL: {b.url}\n")
        return "\n".join(lines)
