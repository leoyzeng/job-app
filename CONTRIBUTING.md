# Contributing

Thank you for helping. Issues and pull requests are welcome. This is a spare-time project, so replies may take a while.

## Set up for development

```
git clone <your fork>
cd job-app
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
```

The tests never use Claude: stand-in agents take its place. They run a real dashboard server and a headless Chromium. The full suite takes about five minutes. GitHub Actions runs it on Windows, macOS and Linux for every pull request.

Set `JOBAPP_HOME` to a scratch folder while you try things, so your own job search data stays apart:

```
JOBAPP_HOME=/tmp/jobapp-dev ./jobapp.sh demo
```

## How the code is laid out

| Folder | What is in it |
|---|---|
| `app/server/` | The dashboard's web server (Starlette) and its API |
| `app/core/` | Jobs and their statuses, settings, ranking, usage caps, updates, diagnostics |
| `app/profile/` | Reading the resume and the requirements |
| `app/search/` | Job sources, the search run, and the agents that plan and judge |
| `app/forms/` | Reading and filling application forms (`reader.js` runs in the page) |
| `app/apply/` | The application agent's tools and the rules it cannot break, answers, the writer |
| `app/agents/` | Starting agents through the Claude Agent SDK, and signing in |
| `app/browser/` | Job App's browser window (Playwright with the installed Chrome) |
| `app/checks/` | `jobapp check` and `jobapp signin` |
| `ui/` | The dashboard (plain HTML, CSS and JavaScript, no build step) |
| `prompts/` | The instructions each agent follows |

## Rules for changes

- Safety rules belong in code (`app/apply/tools.py`, `app/forms/`), not only in prompts. Add a test for each one.
- Nothing personal in the repository: no real resumes, answers, names or links to filled-in forms. The tests use the made-up "Sam Rivera".
- Keep messages to the user plain and specific: say what happened and what to do next.
- `claude-agent-sdk` is pinned below the next minor version. Raise it in its own pull request, after running `jobapp check` with a real sign-in.
