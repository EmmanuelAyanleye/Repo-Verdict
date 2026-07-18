from django.db import models


class AnalysisRequest(models.Model):
    """Simple audit log for analysis requests."""

    repo_url = models.URLField()
    owner = models.CharField(max_length=255)
    repo = models.CharField(max_length=255)
    commit_hash = models.CharField(max_length=64)
    feature_title = models.CharField(max_length=500)
    feature_category = models.CharField(max_length=100)
    verdict = models.CharField(max_length=50)
    confidence = models.FloatField()
    summary = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.owner}/{self.repo} — {self.verdict}"
