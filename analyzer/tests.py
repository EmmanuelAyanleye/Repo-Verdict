from django.test import TestCase

from .analyzer import EvidenceItem, FeatureAnalyzer
from .views import _basic_repo_score, _llm_refine_search_results, _repo_text, _score_repo, _shipd_candidate_score
from repo_verdict.settings import _load_dotenv_file


class AnalyzerSmokeTests(TestCase):
    def test_index_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_analyze_requires_fields(self):
        response = self.client.post(
            "/api/analyze/",
            {"repo_url": "", "commit_hash": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_repo_scoring_accepts_null_github_fields(self):
        repo = {
            "name": "example",
            "description": None,
            "topics": None,
            "language": None,
        }

        self.assertEqual(_repo_text(repo), "example")
        score = _score_repo(repo, {}, [], min_loc=30000, require_architecture=True, allow_surface=False)
        self.assertEqual(score["primary_loc"], 0)
        self.assertEqual(score["score_mode"], "full")

    def test_basic_repo_score_is_visible_metadata_score(self):
        repo = {
            "name": "example",
            "description": None,
            "topics": None,
            "language": "Python",
            "license": {"spdx_id": "MIT"},
            "stargazers_count": 1200,
            "size": 5000,
            "pushed_at": "2026-07-01T00:00:00Z",
        }

        score = _basic_repo_score(repo)
        self.assertEqual(score["score_mode"], "basic")
        self.assertTrue(score["score_available"])
        self.assertGreater(score["suitability_score"], 0)
        self.assertEqual(score["source_dir_count"], 0)

    def test_shipd_score_rewards_verified_coupling(self):
        repo = {"name": "compiler-core", "description": "compiler and type checker"}
        metrics = {"suitability_score": 65}
        coupling = {"coupling_status": "verified"}
        reliability = {
            "reliability_risks": ["No major reliability risks detected from bounded metadata checks."],
            "architecture_docs": ["docs/architecture.md"],
            "ci_checked": 4,
            "ci_successes": 4,
        }

        score = _shipd_candidate_score(repo, metrics, coupling, reliability, "compilers, type checkers")

        self.assertGreater(score, 90)

    def test_llm_search_refinement_falls_back_without_key(self):
        repos = [{"full_name": "owner/repo", "suitability": {"suitability_score": 50}}]

        refined, enhanced = _llm_refine_search_results(repos, "Python", "compilers")

        self.assertFalse(enhanced)
        self.assertEqual(refined, repos)

    def test_env_loader_accepts_utf16_powershell_files(self):
        with self.subTest("utf-16 .env"):
            import os
            import tempfile
            from pathlib import Path

            key = "REPO_VERDICT_TEST_UTF16_VALUE"
            os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text(f"{key}=works\n", encoding="utf-16")
                _load_dotenv_file(path)
            self.assertEqual(os.environ.get(key), "works")
            os.environ.pop(key, None)


class FeatureAnalyzerVerdictTests(TestCase):
    def test_merged_related_fork_pr_counts_as_existing_prior_art(self):
        analyzer = FeatureAnalyzer()
        analyzer._keyword_weights = {"polarplane": 5.0, "scaling": 4.0}
        analyzer._tech_terms = {"polarplane"}

        feature = {
            "title": "Pluggable axis scaling for NumberLine and Axes",
            "description": "Adds PolarPlane and pluggable coordinate scaling.",
        }
        pr = EvidenceItem(
            type="pull_request",
            number=1447,
            title="Add PolarPlane",
            url="https://github.com/ManimCommunity/manim/pull/1447",
            state="merged",
            author="contributor",
            body="",
            relevance=0.72,
            reason="Matched keywords: polarplane",
            matched_keywords=["polarplane"],
            repository="ManimCommunity/manim",
            repository_relation="fork",
        )

        verdict, confidence, summary, details, cited = analyzer._determine_verdict(
            feature, issues=[], prs=[pr], commits=[], branches=[]
        )

        self.assertEqual(verdict, "existing")
        self.assertGreaterEqual(confidence, 0.8)
        self.assertIn("ManimCommunity/manim", summary)
        self.assertEqual(cited, [pr])

    def test_description_terms_are_used_for_topic_matching(self):
        analyzer = FeatureAnalyzer()
        analyzer._keyword_weights = {"polarplane": 5.0}
        analyzer._tech_terms = {"polarplane"}

        item = EvidenceItem(
            type="pull_request",
            number=1447,
            title="Add PolarPlane",
            url="",
            state="merged",
            author="",
            body="",
            relevance=0.7,
            reason="",
            matched_keywords=["polarplane"],
        )
        feature_words = set(
            analyzer._normalize_text(
                "Pluggable axis scaling for NumberLine and Axes. New PolarPlane support."
            ).split()
        )

        self.assertTrue(analyzer._item_matches_topic(item, feature_words))
