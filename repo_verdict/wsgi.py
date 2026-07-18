"""
WSGI config for repo_verdict project.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "repo_verdict.settings")

application = get_wsgi_application()
