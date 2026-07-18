from django.test import TestCase

from .analyzer import EvidenceItem, FeatureAnalyzer


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
