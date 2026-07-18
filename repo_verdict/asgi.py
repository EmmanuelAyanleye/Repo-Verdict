"""
ASGI config for repo_verdict project.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "repo_verdict.settings")

application = get_asgi_application()
