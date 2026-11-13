# Job App

Job App finds job postings that match your resume and your requirements, lists them in a dashboard on your computer, and helps fill in the application forms in a browser window. **It never submits an application.** You review every form and submit it yourself.

It runs on Windows, macOS and Linux. The agents run on Claude, and each person signs in with **their own** Claude account or Anthropic API key.

> Job App is an independent open-source project. It is not made or endorsed by Anthropic.

## What it does

- **Profile and requirements.** Upload a resume (PDF or Word) and an agent reads your details from it. You correct and confirm them. You write your hard requirements and soft preferences in your own words. An agent turns them into search terms, and you confirm what it understood.
- **Job search.** It reads the public job boards of companies you watch and companies a planner agent suggests (Greenhouse, Lever, Ashby, Workday), the community internship and new-grad lists on GitHub, and what two web-search agents find. Every posting is judged against your hard requirements.
  - Postings are ranked by hard requirements first, then soft preferences, then how well the job fits your resume.
  - The same company comes up again only after four others.
  - The search can also run once a day.
- **Autofill.**
  - **Prepare** opens the application in Job App's own browser window, attaches your resume and fills in what your profile and saved answers settle. It leaves the rest for you.
  - It recognises questions by meaning, so "Are you legally allowed to work here?" gets the answer you gave to "Work authorization".
  - Cover letters and long answers can be drafted from your resume. Every fact in a draft is checked against your resume, and drafts are marked as AI-drafted.
- **Rules the agent cannot break.** These are enforced in code, not only in its instructions:
  - It never fills passwords, payment details or ID numbers, and never fills or clicks past a sign-in page.
  - Agreements and signatures are left to you.
  - Work authorization, sponsorship, salary, demographic, criminal record, clearance, age and citizenship questions are only ever answered with your own saved answer.
  - It never clicks Submit, or any button whose purpose is unclear.
- **Limits.** A daily cap on agent usage, plus caps per application and per search.

Job App does not read or fill LinkedIn, Indeed, Glassdoor or similar boards, because their terms forbid automated use. You can still add a job from them by link and fill in the details yourself.

## Requirements

- **Python 3.12 or 3.13.**
  - Windows: [python.org](https://www.python.org/downloads/windows/) or `winget install Python.Python.3.12`.
  - macOS: `brew install python@3.12` or python.org.
  - Linux: your package manager. If [uv](https://docs.astral.sh/uv/) is installed, setup can use it to get Python for you.
- **Google Chrome** (or Microsoft Edge).
- **Git.**
- **One of these, in your own name:**
  - a Claude account with a Pro or Max plan, or
  - an Anthropic API key from the [Claude Console](https://platform.claude.com/settings/keys), billed to your Console account.

## Install

### The double-click way

Clone or download the repository, then open the `scripts` folder:

- **Windows:** in `scripts\Windows`, double-click `1 Set up.bat`, then `2 Sign in.bat`, then `3 Check this computer.bat`. From then on, `Start Job App.bat` starts it.
- **Mac:** do the same with the `.command` files in `scripts/Mac`. Each one opens a Terminal window.
  - The first time, macOS may say the file is from an unidentified developer. Right-click it, choose **Open**, and confirm.
  - If macOS says the file "could not be executed", run `chmod +x scripts/Mac/*.command` once in Terminal, in the Job App folder.

`Start Job App` keeps its window open while Job App runs. Close that window to stop Job App.

### The terminal way

On Windows, use Command Prompt or PowerShell. In PowerShell, type `.\jobapp` instead of `jobapp`.

```
git clone <this repository's URL> job-app
cd job-app
jobapp setup
jobapp signin
jobapp check
jobapp
```

On macOS or Linux, use Terminal:

```
git clone <this repository's URL> job-app
cd job-app
./jobapp.sh setup
./jobapp.sh signin
./jobapp.sh check
./jobapp.sh
```

What each step does:

- **`setup`** creates a private Python environment in `.venv` inside the folder and installs the packages there. The first run is a large download, because it includes the Claude runtime the agents use. Nothing is installed system-wide.
- **`signin`** asks how the agents should sign in (see the next section).
- **`check`** tests your computer. It opens Job App's browser window a few times and reaches the job feeds. It also runs the agents on a practice form, which uses a small amount of your Claude usage. Add `--skip-autofill` to leave the longest agent test out.
- **`jobapp`** (or `./jobapp.sh`) on its own starts Job App and opens the dashboard. Keep the terminal open while you use it. Press Ctrl+C there to stop it.

Try **`jobapp demo`** to look around with made-up data. Its agents are stand-ins, so it uses no Claude usage at all.

### All commands

| Windows | macOS / Linux | What it does |
|---|---|---|
| `jobapp setup` | `./jobapp.sh setup` | Install or repair (run again after updating) |
| `jobapp signin` | `./jobapp.sh signin` | Choose how the agents sign in; `--plan`, `--api-key`, `--status`, `--sign-out` |
| `jobapp check` | `./jobapp.sh check` | Test this computer; `--quick` only looks at what is installed |
| `jobapp` | `./jobapp.sh` | Start Job App |
| `jobapp demo` | `./jobapp.sh demo` | Start with sample data |
| `jobapp where` | `./jobapp.sh where` | Show where your data is |
| `jobapp update` | `./jobapp.sh update` | `git pull`, then setup |
| `jobapp import <folder>` | `./jobapp.sh import <folder>` | Copy your data from an older Job App folder |
| `jobapp forget` | `./jobapp.sh forget` | Delete everything Job App keeps about you |

## Signing in: your own account, your own usage

Every person who runs Job App signs in for themselves. The project has no shared key, and nobody else's account is ever used. `signin` offers two ways:

1. **Your Claude account (Pro or Max plan).**
   - The Claude runtime that comes with the Claude Agent SDK opens your browser, and you sign in there.
   - Agent usage counts against your plan's normal limits.
   - An `ANTHROPIC_API_KEY` set in your environment is ignored in this mode, so it is never billed by surprise.
2. **Your own API key.**
   - Paste a key from the Claude Console. It is kept in your system's password store: Windows Credential Manager, the macOS Keychain, or the Secret Service on Linux. Only if no password store is available is it kept in a file only you can read, in your data folder.
   - The key is passed only to the agents' runtime. It is never shown, logged or written to settings.
   - Usage is billed to your Console account, and the caps in Settings are then real money.

Please note: Anthropic's Agent SDK documentation asks developers of third-party products to use API keys, and not to offer claude.ai sign-in, unless Anthropic has approved it. If you want to be on the safe side, use the API-key option.

Advanced: you can also use your own Amazon Bedrock, Google Vertex AI or Microsoft Foundry account. Choose the API-key option and set the Claude runtime's own environment variables, for example `CLAUDE_CODE_USE_BEDROCK=1` and that provider's credentials. Their documentation has the details.

## Where your data is

Your data stays on your computer, in one folder outside the program folder. Updating the program never touches it.

| System | Folder |
|---|---|
| Windows | `%LOCALAPPDATA%\JobApp` |
| macOS | `~/Library/Application Support/JobApp` |
| Linux | `~/.local/share/job-app` |

Set `JOBAPP_HOME` to use another folder. Settings → **Open my data folder** opens it.

| Inside it | What it holds |
|---|---|
| `profile/` | Your resume as you gave it, its text, your confirmed details (`profile.json`), your requirements in your words, and optional `writing_samples/` for the writing agent to match your voice |
| `config/settings.json` | Your settings, including the companies you watch |
| `data/tracker.json` | Every job and its status (the previous version is kept as `.bak`) |
| `data/answers.json` | Your answers to application questions, learned from what you typed. Edit them on the Answers screen. |
| `data/applications/<job>/` | One folder per application: each field and where its value came from, drafts, and every step taken |
| `data/search_runs/`, `data/postings/` | What each search looked at, and the postings it listed |
| `data/browser_profile/` | Job App's browser window's own profile, with the sign-ins and cookies of sites you use in it. Treat it like a password. Your everyday Chrome profile is never touched. |
| `data/logs/` | `app.log`, `agents.log` (one line per agent run, with no personal content), check reports, and `pip-install.log` |

The Claude runtime also keeps transcripts of agent runs in the `.claude` folder in your home folder. Job App deletes the transcripts of its own runs when it starts and stops, unless you switch on **Keep agent transcripts** in Settings.

`PRIVACY.md` says exactly what is sent where.

### Coming from an older copy

Before 0.9, Job App kept your data inside the program folder. To bring it over, run `jobapp import <old folder>` (or `./jobapp.sh import <old folder>`) once. Use the folder that has `profile`, `data` and `config` in it. The old folder is left as it was.

## Updating

Run `jobapp update` (or `./jobapp.sh update`), which runs `git pull` and then setup. If Settings → **Tell me about new versions** is on, the dashboard shows a notice once a newer release is published on GitHub.

## Costs

Job App is free. Agent usage is yours:

- With a Claude plan, it counts against your plan's limits.
- With an API key, it is billed per use. The defaults cap it at $5 a day, $1 per application and $1.50 per search. Change these in Settings.

## If something goes wrong

1. Run `jobapp check` (or `./jobapp.sh check`). Each failure comes with the fix.
2. Look in `data/logs/` in your data folder.
3. To report a bug, use Settings → **Copy diagnostics** and paste the text into the issue. It leaves out your resume, answers, jobs and page contents. Please never paste those, or a key, into an issue.

## You are responsible for what you submit

Job App fills forms; you decide what is sent. Read every application before you submit it. Some employers ask applicants to disclose AI help, and drafted text is marked so you can tell. The software comes with no warranty (see `LICENSE`).

## Licenses

Job App is under the MIT License (`LICENSE`). The [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) and the Claude runtime it installs come under [Anthropic's terms](https://www.anthropic.com/legal/commercial-terms), not this project's license. Other packages keep their own licenses.

## For developers

- `CONTRIBUTING.md` explains how to run the tests and how the code is laid out.
- `docs/history/` holds the original design plan and build notes.
