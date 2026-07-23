# RepoVerdict

A beautiful Django web tool that analyzes a GitHub repository at a specific commit and tells you whether a proposed feature/enhancement/bugfix/refactor already exists, is open, closed, rejected, or in progress.

## What it does

RepoVerdict takes:

- A GitHub repository URL
- A base commit hash
- A feature description (title, category, description, technical scope, etc.)

It then queries the GitHub API and produces a structured verdict such as:

- **Existing** – the feature already exists in the repo or in an open PR
- **Open** – there is an open issue or PR tracking the same work
- **In Progress** – an open PR is actively implementing the feature
- **Closed** – a related issue/PR was closed (merged or completed)
- **Rejected** – maintainers explicitly declined the feature
- **New** – no relevant existing work was found

Each verdict is backed by cited issues, pull requests, commits, and branches.

The Search page is tuned for Shipd Olympus repository discovery. It keeps the
existing controls for stars, repository size, result count, LOC, licenses and
language, but defaults to the hard constraints: public repos, 500+ stars, one
target language, recent activity, 30k+ primary-language LOC, roughly 60MB or
smaller clone size, and an allowed permissive license. Results are ranked by a
Shipd fit score that combines LOC, architecture depth, recent merged-PR
cross-component coupling, CI/docs/build signals, and optional domain ideas. If
strict checks leave fewer than 20 results, the list stays short rather than
including repos that fail hard requirements.

If `OPENAI_API_KEY` is set, search results get an additional AI refinement pass:
the shortlist is reranked and its coupling/risk/gap notes are rewritten from the
GitHub metadata already collected. The key stays server-side and is never sent
to browser JavaScript. If the OpenAI call fails or no key is configured, search
falls back to the normal heuristic ranking.

## Tech stack

- **Backend**: Python, Django, Django REST Framework
- **Frontend**: HTML5, CSS3, vanilla JavaScript
- **Analysis**: GitHub REST/GraphQL API + optional OpenAI LLM enhancement
- **HTTP client**: `requests`

## Quick start

1. Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# or: venv\Scripts\activate  # Windows
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy the environment example and add your keys (optional but recommended):

```bash
cp .env.example .env
```

On Windows PowerShell, use:

```powershell
Copy-Item .env.example .env
Set-Content -Path .env -Value "GITHUB_TOKEN=your_github_token_here`nOPENAI_API_KEY=your_openai_api_key_here" -Encoding utf8
```

After editing `.env`, restart `python manage.py runserver`.

4. Run migrations:

```bash
python manage.py migrate
```

5. Run smoke tests:

```bash
python manage.py test analyzer.tests
```

6. Start the development server:

```bash
python manage.py runserver
```

7. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

Do not open files from `templates/` directly in the browser; run the Django
server and open the localhost URL so CSS, JavaScript, and API routes load
correctly. Static assets are served by Django in development and by WhiteNoise
when `DEBUG=False`.

## API usage

### `POST /api/analyze/`

Request body:

```json
{
  "repo_url": "https://github.com/FoundationAgents/OpenManus",
  "commit_hash": "52a13f2a57d8c7f6737eefb02ccf569594d44273",
  "feature": {
    "title": "Adaptive Memory Compaction for Token Budget Management",
    "category": "Feature Request",
    "description": "OpenManus agents currently fail with TokenLimitExceeded...",
    "technical_scope": "New subsystem: app/memory/compaction.py..."
  }
}
```

Response:

```json
{
  "verdict": "open",
  "confidence": 0.82,
  "summary": "A related token-limit issue (#794) was merged, but no adaptive compaction strategy exists yet.",
  "evidence": {
    "issues": [...],
    "pull_requests": [...],
    "commits": [...],
    "branches": [...]
  },
  "details": [...]
}
```

## Configuration

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Enables LLM-powered semantic matching and verdict reasoning. |
| `GITHUB_TOKEN` | Raises GitHub API rate limits from 60/hr to 5,000/hr. |
| `DEBUG` | Django debug mode. Set to `False` in production. |
| `SECRET_KEY` | Django secret key. Change in production. |
| `ANALYZE_RELATED_REPOS` | Enables parent/source/fork/same-name repository checks for public prior art. Defaults to `True`. |
| `RELATED_REPO_LIMIT` | Maximum related repositories to inspect. Defaults to `4`. |
| `RELATED_FORK_MIN_STARS` | Minimum stars for related fork/name candidates. Defaults to `100`. |
| `RELATED_SEARCH_TERMS` | Number of distinctive feature terms to search per repository. Defaults to `8`. |

## License

MIT
