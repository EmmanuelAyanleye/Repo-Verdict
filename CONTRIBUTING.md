# Contributing to RepoVerdict

Thanks for wanting to help. RepoVerdict is a small Django project, so it's a good place to make your first open-source contribution.

## Ways to help

- **Report a bug.** Open an issue with the repository URL, the commit and the feature you checked, what you expected and what you got.
- **Suggest an idea.** Open an issue first so we can talk it through before you write any code.
- **Fix something.** Pick an issue labelled `good first issue` or `help wanted`, or any open issue, and leave a comment so others know you're on it.
- **Improve the docs.** Clearer setup steps and better examples help everyone.

## Run it locally

You need Python 3.10 or newer.

```bash
git clone https://github.com/<your-username>/Repo-Verdict.git
cd Repo-Verdict
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

Then open http://127.0.0.1:8000.

The keys in `.env` are optional:

- Without `GITHUB_TOKEN` you get 60 GitHub API requests an hour, which runs out quickly. A free personal access token with no extra scopes is enough.
- Without `OPENAI_API_KEY` everything still works, using keyword matching instead.

## Making a change

1. Fork the repo and create a branch from `main` with a short, clear name, like `fix-null-topics`.
2. Keep each pull request to one change. Small pull requests get reviewed faster.
3. Run the tests before you push:

   ```bash
   python manage.py test analyzer.tests
   ```

4. If you fix a bug, add a test that would have caught it.
5. Open a pull request that says what you changed and why. For anything you can see in the browser, add a screenshot.

## Code style

- Follow the style of the code around your change.
- Keep functions small and names clear.
- Never commit `.env`, API keys or tokens.

## Questions

Open an issue and ask. There are no silly questions.

By contributing, you agree that your work is released under the [MIT License](LICENSE).
