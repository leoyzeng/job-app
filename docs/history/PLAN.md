# Job App: implementation plan

Status: all seven checkpoints built, plus the 0.8.0 changes (version 0.8.0). What was built, and what has been tried on the PC, is in `docs/CHECKPOINTS.md`.
Written 2026-11-10, when the app was built for one Windows PC.
Companion file: `docs/AGENT_BRIEFS_DRAFT.md` (the instructions the app's agents will follow).

---

## 1. Summary

Job App is a program that runs on your PC. It finds job postings that match your requirements, lists them in a dashboard, and for each job you pick it opens the application form in a browser window, fills in what it can from your resume and from answers you have given before, and stops whenever it is unsure. It never submits an application. You review and submit yourself.

### Decisions from your answers

| Topic | Decision |
|---|---|
| What runs the agent | A standalone program on your PC that launches its own Claude agents. No chat needs to be open. |
| Browser | Your choice, in Settings: **Claude's browser window** (a dedicated window the app controls, the default) or **your own Chrome**. Added 2026-11-11 at your request. See the note below. |
| Job sources | Company career pages, companies you name, other job boards, LinkedIn, and any other legitimate source on the web. |
| Requirements | A dashboard section asks for your resume, your hard requirements and your soft preferences, in plain language. Hard requirements filter. Soft preferences rank. |
| Buttons | Prepare starts the autofill. Continue resumes after any pause. Skip and Mark as applied work as you described. |

### One thing I had to adapt

You chose "standalone program" and "Claude's built-in browser first". Those two cannot be combined: the built-in browser is a pane inside the Claude desktop app, and only a Claude chat can drive it. A standalone program cannot reach it.

The plan uses the closest equivalent: a **dedicated browser window** that the app opens and controls. Like the built-in browser it is separate from your everyday Chrome and keeps its own sign-ins between runs. Unlike the built-in browser, it can attach your resume file to a form automatically.

**Update, 2026-11-11.** At your request, where job postings and applications open is now a setting with two choices: **Claude's browser window** (the dedicated window above, and the default) or **your own Chrome** (a new tab in the Chrome you use every day). Opening postings works in both now. Autofill in your own Chrome needs a small Job App helper extension that you add once; it is built with the form engine in Phase 4.

If you would rather have the built-in browser, the engine has to be a Claude chat instead of a standalone program. Tell me and I will rework the plan.

### What I need from you before building

1. **Resume.** Put a PDF or DOCX in `job_app\profile\`. (Once the dashboard exists you can also upload it there.)
2. **How the app pays for Claude.** Either sign in with your Claude plan, or use an API key. Details in section 3.4.
3. **Python.** Run `py --version` in a terminal and tell me the result. The app needs 3.10 or newer.
4. **A browser.** Confirm Chrome or Edge is installed (Edge ships with Windows).
5. Optional: a list of target companies, and one or two things you have written (a past cover letter, an essay) so drafted answers can sound like you.

---

## 2. Requirements as I understand them

Each requirement has an ID so later sections and tests can point back to it.

| ID | Requirement |
|---|---|
| R1 | Your resume is the starting point for everything the app knows about you. |
| R2 | You state what jobs you want in plain language, split into hard requirements ("must be an internship or entry level") and soft preferences ("prefer robotics", "prefer pay of at least X"). The app saves this to a file and uses it to find and filter jobs. A dashboard section prompts you for the resume and these requirements. |
| R3 | A GUI lists all jobs and has a button to search for new ones. |
| R4 | Each listing shows company name, job title, link, description and other relevant details. |
| R5 | Each listing has four buttons: Skip, Prepare, Continue, Mark as applied. |
| R6 | A file records every job and whether it is listed, skipped or applied. |
| R7 | Skip removes the listing, records it, and it never shows up again. |
| R8 | Prepare opens the browser at the job link and starts autofilling. |
| R9 | Autofill uses the resume and known answers to fill as much as possible. A file records every field encountered and the answer given. |
| R10 | When the agent is stuck or unsure it pauses. The listing says "Waiting for input". You type the answer into the browser yourself. |
| R11 | When you click Continue, the app checks the browser, finds the fields you changed, and saves those answers so it can reuse them. Over time this becomes a repository of your answers. |
| R12 | When everything is filled, the app does not submit. The listing says "Waiting for review". |
| R13 | You can mark any job as applied yourself, for example after applying by hand. |
| R14 | Setting: pause after each page of an application so you can review it before the agent moves on. |
| R15 | Setting: let the agent draft cover letters and long written answers, in a voice that sounds like you. |
| R16 | The agents that fill applications get very specific written instructions. |
| R17 | Search may use any legitimate source on the internet. |
| R18 | The app is a standalone program. |

---

## 3. Architecture

### 3.1 Overview

Everything runs in one Python process on your PC (R18).

```
┌─────────────────────────── Job App (one process) ───────────────────────────┐
│                                                                              │
│   Dashboard (web page in its own window)                                     │
│        ▲  buttons, live status                                               │
│        ▼                                                                     │
│   Local server ──► Job store ──► files in job_app\ (tracker, answers, ...)   │
│        │                                                                     │
│        ├──► Agent runner ──► Claude (via the Claude Agent SDK)               │
│        │         │  the agent can only act through the app's own tools       │
│        │         ▼                                                           │
│        └──► Browser manager ──► Claude's browser window, or your Chrome      │
│                                  (you can type in it at the same time)       │
└──────────────────────────────────────────────────────────────────────────────┘
```

The important design choice: **the app owns the browser, not the agent.** The agent asks the app to do things ("fill these fields", "show me the page") through a small set of tools the app defines. This is what makes three of your requirements dependable:

- The browser stays open while the agent is paused, so you can type in it (R10).
- The app itself reads the form before and after you type, so detecting what you changed is done by code, not by an AI guessing (R11).
- The app has no "submit" tool to give the agent, so it cannot submit (R12).

### 3.2 Components

| Component | What it does |
|---|---|
| Dashboard | The GUI. Lists jobs, shows status, holds the buttons, the profile and requirements section, the answers page and settings. |
| Local server | Serves the dashboard on `127.0.0.1` only, receives button clicks, pushes live status updates. |
| Job store | Reads and writes the data files. The only component that writes them, so files never get corrupted by two writers. |
| Search pipeline | Collects postings from sources, removes duplicates and anything already skipped or applied, checks them against your requirements, adds the survivors to the list. |
| Browser manager | Opens pages where the browser setting says. Claude's browser window: a dedicated window with a profile folder that keeps your sign-ins, kept open and reused, one tab per posting or application. Your own Chrome: a new tab in your everyday Chrome; from Phase 4 the form engine reaches it through the Job App helper extension. |
| Form engine | Plain code, no AI. Reads every field on a page (label, type, options, current value), fills fields, verifies the result, compares two readings of a page to find what changed. |
| Agent runner | Starts a Claude agent for a task, hands it the instructions and tools, enforces limits, records cost. |
| Answer repository | Stores your answers and finds the saved answers that fit a new question. |

### 3.3 Technology

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | One language for server, browser control and agents. |
| Agents | Claude Agent SDK for Python (`claude-agent-sdk`) | Runs the agent loop for us and lets the app define its own tools in-process. The package ships with the Claude Code runtime inside it, so no separate install (confirmed on Linux; Phase 0 confirms the Windows package). |
| Browser control | Playwright for Python, driving your installed Chrome or Edge | Reliable form reading and filling, file upload, works with pages inside frames. Uses a profile folder inside `job_app\data\` so sign-ins persist. |
| Server | Starlette + Uvicorn, server-sent events for live updates | Small and dependency-light. |
| Dashboard | Plain HTML, CSS and JavaScript, no build step | Nothing extra to install; easy to change. Opens in its own window via Edge's app mode, with your default browser as fallback. |
| Resume reading | `pypdf` and `python-docx`, with Claude reading the file directly as a fallback | Covers PDF and Word. |
| Data | JSON and Markdown files | You asked for files, and you can open and edit them. |
| Tests | `pytest` | Standard. |

I checked the SDK's actual interface (version 0.2.172) rather than relying on memory. The features this plan depends on exist: in-process tools (`tool`, `create_sdk_mcp_server`), tool allow-lists, a permission callback (`can_use_tool`), pre-tool hooks, per-run spending caps (`max_budget_usd`), turn caps, structured output, session resume, and cost reporting on each result.

### 3.4 How the app pays for Claude

Two supported ways. The app uses whichever is configured and shows which one is active.

- **Claude plan sign-in.** You sign in once on your PC. Anthropic's help article says that when you are signed in with your plan, Agent SDK usage draws from your subscription limits. This is the simplest option if you have headroom on your plan.
- **API key.** You put a key from the Claude Console in a `.env` file. Usage is billed per use. The same article says Max, Ultra and Team plans now include monthly API credits that cover the Agent SDK when it runs with a key from your linked Console organization.

Billing rules for this have changed more than once in 2026, so check the article before choosing: <https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan>.

One more note from the SDK documentation: products offered to other people must use API keys, not claude.ai sign-in. This is a personal tool on your own machine, but if you ever share it, switch to API keys.

**Cost controls built into the app:** a spending cap per application, per search and per day; the dashboard shows what each run cost. Simple fields (name, email, phone, links) are filled by plain code without calling Claude at all. Actual cost per application will be measured in Phase 5, and the caps tuned then.

---

## 4. Files and data

### 4.1 Folder layout

```
job_app\
  PLAN.md                     this plan
  docs\AGENT_BRIEFS_DRAFT.md  draft agent instructions
  setup.bat, run.bat          install and start
  requirements.txt, .env.example, .gitignore
  app\                        program code
    server\                   web server, routes, live events
    core\                     data models, file store, job state machine, settings
    profile\                  resume reading, profile building, requirements
    search\                   sources, pipeline, duplicate removal, evaluation
    browser\                  browser manager, form engine, change detection
    agents\                   agent runner, the agents' tools, guardrails
    answers\                  answer repository and matching
  prompts\                    agent instruction files (final versions of the briefs)
  ui\                         dashboard page
  profile\                    YOUR INPUTS
    resume.pdf                your resume as you gave it
    resume.md                 its text
    profile.json              facts about you, extracted and confirmed by you
    requirements.md           your hard requirements and soft preferences, in your words   (R2)
    search_profile.json       the app's structured reading of requirements.md
    writing_samples\          optional samples of your writing
  data\                       WHAT THE APP RECORDS
    tracker.json              every job and its status                                     (R6)
    answers.json              the answer repository                                        (R11)
    applications\<job id>\    one folder per application:
      fields.json               every field encountered and what was entered               (R9)
      snapshots\                readings of the form over time
      events.log                everything the agent did, in order
      drafts\                   cover letter and long answers drafted for this job
    search_runs\              a log per search: what was searched, found, rejected and why
    company_boards.json       career-page addresses learned for companies
    site_notes\               what the app has learned about specific application sites
    browser_profile\          the dedicated browser's sign-ins and cookies
    logs\
  config\settings.json        global settings                                              (R14, R15)
  tests\
```

### 4.2 The tracker (`data\tracker.json`)

One record per job. Records are never deleted, which is how a skipped or applied job is kept from showing up again (R7).

```json
{
  "id": "j_3f9a1c",
  "company": "Acme Robotics",
  "title": "Robotics Software Intern",
  "location": "Boston, MA (hybrid)",
  "url": "https://boards.greenhouse.io/acme/jobs/123",
  "apply_url": "https://boards.greenhouse.io/acme/jobs/123#app",
  "source": { "kind": "greenhouse", "found_via": "watchlist", "search_run": "2026-11-12T09-00" },
  "posted_at": "2026-11-08",
  "found_at": "2026-11-12T09:03:11",
  "description_summary": "Three or four sentences on the role.",
  "details": { "pay": "$32-38/hr", "type": "Internship", "remote": "hybrid", "deadline": null },
  "match": {
    "hard": [ { "requirement": "internship or entry level", "result": "pass", "evidence": "Title says Intern" } ],
    "soft_score": 82,
    "soft_hits": ["robotics", "pay above your preference"],
    "concerns": ["sponsorship not mentioned"]
  },
  "dedupe_keys": ["url:boards.greenhouse.io/acme/jobs/123", "key:acme robotics|robotics software intern|boston"],
  "status": "waiting_for_input",
  "status_detail": {
    "message": "2 fields need you",
    "needs": [ { "label": "Salary expectation", "page": 1, "reason": "no saved answer" } ]
  },
  "history": [ { "at": "2026-11-12T09:10:02", "from": "new", "to": "queued", "by": "user" } ],
  "application": { "folder": "data/applications/j_3f9a1c", "page": 1, "cost_usd": 0.0 }
}
```

### 4.3 Per-application field record (`fields.json`)

One entry per field the agent encountered (R9).

```json
{
  "field_key": "f_81c2",
  "page": 1,
  "label": "Are you legally authorized to work in the United States?",
  "type": "radio",
  "options": ["Yes", "No"],
  "required": true,
  "value": "Yes",
  "source": "saved_answer",
  "answer_id": "a_0019",
  "filled_by": "agent",
  "verified": true,
  "user_changed": false
}
```

`source` is one of: `profile` (a fact from your confirmed profile), `resume`, `saved_answer` (from the repository), `ai_draft` (written by the agent, R15), `user` (you typed it), `site_prefill` (the site filled it itself, for example by parsing your uploaded resume).

### 4.4 The answer repository (`data\answers.json`)

```json
{
  "id": "a_0019",
  "question": "Are you legally authorized to work in the United States?",
  "also_asked_as": ["Do you have authorization to work in the US?"],
  "field_type": "choice",
  "answer": "Yes",
  "source": "user",
  "sensitivity": "sensitive",
  "scope": "global",
  "times_used": 4,
  "first_seen": { "job": "j_3f9a1c", "at": "2026-11-12" },
  "history": []
}
```

- `source`: `user` (you typed it), `user_correction` (you changed what the agent entered), `profile`.
- `sensitivity`: `normal`, or `sensitive` for work authorization, sponsorship, salary, demographic and similar questions. Sensitive answers are only ever reused exactly as you gave them, never guessed.
- `scope`: `global` (reusable anywhere) or `job_specific` (for example "Why do you want to work at Acme?"). Job-specific answers are kept as examples of your writing, not pasted into other applications.

The dashboard has an Answers page where you can read, edit and delete entries.

### 4.5 Requirements (`profile\requirements.md`)

Your own words, in two sections, exactly as you type them in the dashboard:

```markdown
## Hard requirements
- Must be an internship or entry level.

## Soft preferences
- Prefer pay of at least $30 an hour.
- Prefer the job to be related to robotics.
```

The app also keeps a structured reading of this in `search_profile.json` (target titles, keywords, locations, pay floor, and how each hard requirement will be tested). The dashboard shows you that reading so you can correct it. Your wording decides the split: "must", "only" and "required" make a hard requirement; "prefer" and "ideally" make a soft one. If a line is ambiguous the dashboard asks which you meant.

### 4.6 Settings (`config\settings.json`)

```json
{
  "pause_after_each_page": false,
  "write_long_answers": true,
  "browser": { "mode": "dedicated", "channel": "chrome" },
  "models": { "autofill": "sonnet", "search": "sonnet", "writer": "opus" },
  "budget_usd": { "per_application": 1.5, "per_search": 2.0, "per_day": 10.0 },
  "search": { "max_new_per_search": 25, "strict_hard_requirements": false, "watchlist": [], "sources": {} },
  "max_parallel_applications": 1
}
```

The first two are the settings you asked for. `browser.mode` is the third: `dedicated` (Claude's browser window) or `own_chrome` (your own Chrome). The model choices and budget numbers are placeholders until real costs are measured.

---

## 5. Job lifecycle

### 5.1 Statuses

| Status | Shown as | Meaning |
|---|---|---|
| `new` | New | Found by a search, waiting for your decision. |
| `skipped` | (hidden) | You were not interested. Never listed again. |
| `queued` | Queued | You clicked Prepare; waiting for the browser to be free. |
| `filling` | Autofilling, page 2 of 4 | The agent is working. |
| `waiting_for_input` | **Waiting for input** | The agent needs you. The listing says exactly what it needs. |
| `waiting_for_page_review` | Waiting for page review | A page is done and "pause after each page" is on. |
| `waiting_for_review` | **Waiting for review** | Everything is filled. Review and submit in the browser. |
| `applied` | Applied | You marked it applied. Never listed as new again. |
| `closed` | Posting closed | The posting was taken down. |
| `error` | Problem | Something failed. The listing says what, and Prepare retries. |

### 5.2 Flow

```
                 Skip                                   Mark as applied (from any status)
        new ───────────► skipped                               │
         │                                                     ▼
         │ Prepare                                          applied
         ▼
      queued ──► filling ──┬──► waiting_for_input ───────┐
                    ▲      │                             │ Continue
                    │      ├──► waiting_for_page_review ─┤
                    └──────┼─────────────────────────────┘
                           │
                           └──► waiting_for_review ──(you submit in the browser)──► Mark as applied
```

### 5.3 Buttons

| Button | Available when | What it does |
|---|---|---|
| Skip | Any status except applied | Sets `skipped`, hides the listing, closes its browser tab if one is open. (R7) |
| Prepare | `new`, `error` | Opens the application in the browser chosen in Settings and starts autofilling. (R8) |
| Continue | Any "waiting" status | Reads the form, records what you changed, then carries on. In `waiting_for_review` it re-checks the form and records any edits you made, and stays in review. (R11) |
| Mark as applied | Always | Sets `applied`. If the form is still open, the app takes a last reading first so any final edits you made are learned. (R13) |

While a listing is in review, the app watches the tab. If it sees a confirmation page ("Thank you for applying"), the listing offers a one-click "Looks submitted. Mark as applied?".

---

## 6. The dashboard

### 6.1 Screens

1. **Jobs** (main screen)
   - Top bar: **Search for new jobs**, **Add a job by link**, search progress, today's spend.
   - Tabs: New, In progress, Waiting on you, Applied. Skipped is hidden behind a link, with an Undo for each.
   - Each listing (R4): company, title, location, pay if known, posting date, source, link, a short description, a match score with reasons (which hard requirements passed, which soft preferences hit, any concerns), the status, and the four buttons (R5).
   - When a listing is waiting on you it shows the reason in plain words: "Waiting for input: Salary expectation; Sign in to continue."
   - Each listing expands to show the fields filled so far with where each value came from, any drafted text, and a live log of what the agent is doing.
2. **Profile and requirements** (R1, R2). Opens automatically on first run.
   - Upload resume. The app shows the facts it extracted (contact details, education, experience, links) for you to confirm or fix.
   - Two text boxes: hard requirements and soft preferences, with your examples as placeholder text.
   - "Here is how I read that": the structured interpretation, editable.
3. **Answers**: the answer repository. Search, edit, delete, mark sensitive.
4. **Settings**: the two toggles you asked for, browser choice, models, spending caps, sources, watchlist of companies.

### 6.2 Behaviour

- Status changes appear live without refreshing.
- New listings appear one by one while a search is running.
- The dashboard works on its own if the agent side is not configured yet: you can still add jobs by link, skip, and mark as applied.

### 6.3 Local safety

The server listens only on your own machine (`127.0.0.1`) and requires a random token created at startup, so other programs and web pages cannot press its buttons.

---

## 7. Finding jobs

### 7.1 Sources

You approved all source types, with the condition that sources be legitimate. The plan tiers them by reliability:

| Tier | Source | How it is read |
|---|---|---|
| 1 | Career pages of companies on your watchlist, and of companies the app has discovered | Many employers host their jobs on applicant systems (Greenhouse, Lever, Ashby and others) that publish a public feed of open jobs. Plain code reads these. Fast, free, accurate. |
| 2 | Public lists and feeds, such as community-maintained internship and new-grad lists, government job sites, and boards that offer a public feed | Plain code, one small reader per source. Each source is checked for availability and terms before being switched on. |
| 3 | The open web | A search agent runs web searches built from your requirements, opens promising results, and extracts postings. |
| 4 | Job boards and LinkedIn | See 7.4. |

Whenever the app finds a company with a public job feed, it saves that address to `company_boards.json`. Later searches read that company through Tier 1, so the app's coverage grows with use.

**As built in checkpoint 3.** Tier 1 reads Greenhouse, Lever (both its US and EU hosts), Ashby and Workday. A company's board is found by trying likely board names on Greenhouse, Lever and Ashby, or learned from any posting address the search reads; Workday boards can only be learned from addresses. Tier 2 is the community lists kept on GitHub by Simplify and the Pitt Computer Science Club (`Summer<year>-Internships`, `New-Grad-Positions`), read from their `listings.json`. Tier 3 is two web search agents with WebSearch and the app's own page reader. Pages are read through a reader that refuses this PC and the local network, and pages drawn by JavaScript are shown in a hidden browser.

### 7.2 Pipeline

Clicking **Search for new jobs** runs these steps. Progress shows in the top bar.

1. **Plan.** Turn `search_profile.json` into search queries and a list of feeds to read.
2. **Collect.** Read Tier 1 and 2 sources with code. In parallel, start search agents for Tier 3 and 4, each covering one cluster of queries. Each returns candidates in a fixed format.
3. **Normalize.** Resolve each candidate to the employer's own posting when possible, since that is where the application form is.
4. **Remove duplicates.** Drop anything already in the tracker in any status, including skipped and applied (R7). Matching uses the link and a company + title + location key, so the same job found on two sites counts once.
5. **Check it is real and open.** See 7.3. Open the posting to confirm it still accepts applications.
6. **Evaluate against your requirements.** For each candidate an evaluator reads the full description and returns: pass, fail or unknown for each hard requirement with the sentence that proves it; a 0-100 score for soft preferences with reasons; any concerns.
   - Fails a hard requirement: not listed.
   - Unknown on a hard requirement (for example pay not stated): listed with a visible "could not verify" note. A setting makes this strict.
7. **List.** Add to the tracker as `new`, sorted by score.
8. **Log.** Write `search_runs\<time>.json` with every query, source, count and rejection reason, so you can see why something did or did not appear.

**Add a job by link** runs steps 3 to 7 on one address you paste.

### 7.3 Legitimacy checks (R17)

A posting is listed only if:

- it sits on the employer's own site, on a recognized applicant system, or on a reputable board;
- the employer can be identified and the apply link leads to that employer or its applicant system;
- it shows none of the usual scam signs: fees to apply or for training, contact only through a personal email or messaging app, requests for bank details, pay far out of line with the role, mismatched web addresses.

Anything borderline is listed with an "unverified employer" note rather than silently dropped or silently trusted.

### 7.4 Job boards and LinkedIn

These need care, and I want to be plain about it. LinkedIn, Indeed and Glassdoor restrict automated access in their terms, and LinkedIn can limit accounts it believes are being automated. So the plan is:

- **What the app does by default.** It uses postings from these sites that are publicly readable and surface through ordinary web search, then looks for the same job on the employer's own site and links to that. Most postings on these boards point to the employer's site for the actual application.
- **You browse, the app captures.** When you are looking at a job on LinkedIn or any board in the dedicated browser, an "Add this page" button adds that one posting to your list. You do the browsing; the app reads only the page you chose.
- **What the app does not do by default.** It does not crawl LinkedIn search results while signed in as you, and it does not automate LinkedIn's in-site "Easy Apply". Both are possible to build as opt-in features in Phase 7, and both carry real risk to your account. I recommend deciding after you have used the rest.

For jobs that can only be applied to inside a board, apply by hand and click Mark as applied.

---

## 8. Filling an application

### 8.1 What happens when you click Prepare

```
Prepare
 ├─ open a tab at the job's application link
 ├─ check for blockers: sign-in wall, account creation, CAPTCHA, posting closed
 │    └─ any blocker that needs you ─► Waiting for input ("Sign in, then click Continue")
 └─ work the page  ◄──────────────────────────────────────────────────────────┐
      1. read the page: every field, its label, type, options, current value   │
      2. if there is a resume upload, attach the resume first and wait,        │
         because many sites then fill some fields themselves; read again       │
      3. plain code fills the simple, certain fields (name, email, phone, ...) │
      4. the agent handles everything else: pick a saved answer, use a fact    │
         from your profile, draft a long answer (if enabled), or flag the      │
         field as needing you                                                  │
      5. read the page again and check every value took; fix or flag           │
      6. save this reading as "what the agent left"                            │
      7. decide:                                                               │
           a required field needs you          ─► Waiting for input            │
           page done, "pause after each page"  ─► Waiting for page review      │
           page done, more pages               ─► click Next ──────────────────┘
           last page done                      ─► Waiting for review
```

### 8.2 What happens when you click Continue

1. The app reads the form again.
2. It compares this reading with "what the agent left". Every field whose value differs is something you changed or filled (R11).
3. Each of those is written to `fields.json` (source `user`) and saved to the answer repository with the question text, your answer, the field type and its options.
   - If you changed a value the agent had entered, that is saved as a correction. The old answer for that question is replaced and kept in history, so the same mistake is not repeated.
4. If a required field is still empty, the listing stays in Waiting for input with the updated list.
5. Otherwise the agent carries on from where it stopped.

**You do not lose typing if something goes wrong.** While a listing is waiting, the app quietly re-reads the form every few seconds. If the browser is closed or the PC restarts, your entries up to that point are already saved, and Prepare can refill the form from the record.

### 8.3 The tools the agent gets

The agent can do nothing except call these. It has no file, shell or general web access.

| Tool | What it does | Built-in limits |
|---|---|---|
| `get_page` | Returns a compact description of the current page: step indicator, headings, every field (label, type, required, options, current value, error message), buttons, and for each field any suggested value with its source. | Read-only. |
| `fill_fields` | Fills a batch of fields and verifies each. Records each in `fields.json`. | Refuses password, payment and government-ID fields, and consent or attestation checkboxes. Those go on the "needs you" list. |
| `upload_document` | Attaches the resume, a drafted cover letter, or another file you placed in `profile\`. | Only files from `profile\` and this application's `drafts\`. |
| `draft_long_answer` | Asks the writing agent for a draft of a long answer or cover letter, then fills it and marks it as AI-drafted. | Only when "write long answers" is on. Otherwise the field goes on the "needs you" list. |
| `click` | Clicks a button for a stated purpose: open the application form, go to the next page, add another entry (for example a second school), expand a section, dismiss a banner. | Refuses anything that looks like a final submit. If it cannot tell "next" from "submit", it refuses and asks you to click. |
| `need_user` | Adds a field or task to the "needs you" list with a reason in plain words. | |
| `screenshot`, `inspect_element` | Lets the agent look at the page or one element when the description is not enough. | Read-only. |
| `save_site_note` | Records something learned about this site ("the degree dropdown needs typing before options appear") for future applications there. | Notes only. |
| `end_leg` | Ends the agent's turn with an outcome: needs input, page done, ready for review, blocked, or failed. | The app checks the claim against the real page. The agent cannot say "ready for review" while a required field is empty and unflagged. |

Each run of the agent covers one stretch of work, from Prepare or Continue to the next pause. It starts fresh each time with the instructions, the current page, and the record so far. That keeps every run small and cheap, and means a restart loses nothing.

### 8.4 How the agent decides what goes in a field

In order:

1. A saved answer you gave to the same question: use it.
2. A fact from your confirmed profile: use it.
3. Something that follows unambiguously from the resume (most recent employer, degree, graduation date): use it.
4. A long written answer, and drafting is enabled: draft it (8.6).
5. Anything else: leave it blank and put it on the "needs you" list.

The agent never invents facts. A blank field costs you ten seconds. A wrong one can cost you the application.

**Required versus optional.** The agent pauses for required fields it cannot answer. Optional fields it cannot answer are left blank and listed in the review notes, so the app does not stop every few seconds.

**Matching a new question to a saved answer.** Code finds candidates by comparing the wording. The agent then judges whether the questions really mean the same thing ("Are you legally authorized to work in the US?" and "Do you have US work authorization?" do; "Will you now or in the future require sponsorship?" is a different question). It maps the answer onto the options the form offers. A new wording is only added to an answer after you have seen it in review and left it unchanged.

### 8.5 Sensitive questions

These are never guessed, whatever the resume implies:

- work authorization and visa sponsorship
- salary expectations
- gender, race, ethnicity, veteran status, disability, age, date of birth
- criminal record, security clearance
- anything asking you to certify, attest or agree

The first time one appears, the agent leaves it for you. Once you have answered and clicked Continue, your answer is saved and reused word for word next time. Voluntary demographic questions are usually optional, so they do not cause a pause; they are listed in the review notes, and once you have filled them once they are reused.

Government ID numbers, bank details and passwords are never stored and never filled. You always enter those.

### 8.6 Cover letters and long answers (R15)

When the setting is on, a separate writing agent drafts the text. It receives your resume, the job description, the question, any length limit, your writing samples, and long answers you have written or edited before.

- It may only use facts from your resume and profile. No invented projects, numbers or experience.
- The draft is filled into the form, marked "AI-drafted" in the dashboard, and saved in the application's `drafts\` folder.
- When you edit a draft in the browser, your version is saved as a new example of your voice, so drafts improve as you use the app.

When the setting is off, long-answer fields go on the "needs you" list.

### 8.7 Pause after each page (R14)

- On: after finishing each page the agent stops at Waiting for page review. You look it over, change what you like, click Continue. Your changes are learned and the agent moves to the next page.
- Off: the agent clicks Next itself and only stops when it needs you or reaches the end.
- On a single-page form, the page review and the final review are the same stop.

### 8.8 Final review (R12)

When the last page is filled the listing shows **Waiting for review**, and the dashboard shows a review summary:

- every field and value, with where it came from;
- things to look at: AI-drafted text, optional fields left blank, anything the agent was less sure of, consent boxes left for you to tick.

You submit in the browser yourself, then click Mark as applied (or accept the app's "Looks submitted" prompt).

### 8.9 Which application sites first

Application forms differ a lot between the systems employers use. Order of work:

1. Greenhouse, Lever, Ashby. Single-page forms, very common for startups and tech and robotics companies. First target.
2. Workday. Multi-page, requires an account, unusual controls. Second target, in Phase 7.
3. Others (iCIMS, SmartRecruiters, Taleo, SuccessFactors and so on) as they come up.

On a site the app has not seen before, the agent still tries using the general form engine. It will simply pause more often. `site_notes\` captures what it learns.

---

## 9. The agents

"Subagent" in your description maps to these. The app starts each as a separate Claude agent with its own instructions and only the tools it needs. Full draft instructions are in `docs/AGENT_BRIEFS_DRAFT.md`.

| Agent | Started when | Input | Output |
|---|---|---|---|
| **Application agent** | Prepare or Continue, one per stretch of work on one application | Job details, settings, the page, the record so far | Fields filled, a "needs you" list, an outcome |
| Writing agent | The application agent requests a long answer | Resume, job description, question, your writing samples | Draft text |
| Search planner | Search button, once per change of requirements (cached for 14 days) | Your confirmed requirements | Web queries, title and place filters, community lists to read, companies to look up |
| Search agent | Search button, two in parallel | A cluster of queries | Candidate postings in a fixed format |
| Fit evaluator | For each candidate posting, up to 4 per run | Posting text, your requirements | Hard-requirement verdicts, soft score, reasons |
| Requirements reader | You save your requirements | `requirements.md` | `search_profile.json` |
| Profile builder | You upload a resume | Resume text | `profile.json` for you to confirm |

The application agent's brief is the detailed one, since that agent acts on your behalf on other people's websites (R16).

---

## 10. Guardrails

The agent's instructions tell it what not to do. These limits are also enforced by code, so they hold even if the agent misreads its instructions or a web page tries to mislead it.

| Risk | Enforcement |
|---|---|
| Submitting an application | No submit tool exists. `click` refuses buttons that are or might be a final submit. |
| Entering credentials or financial details | `fill_fields` refuses password, payment-card, bank and government-ID fields. |
| Agreeing to things on your behalf | Consent, certification and attestation checkboxes are refused and left for you. |
| Creating accounts, signing in, CAPTCHAs | The agent stops and asks you. The app does not try to get around bot checks. If a site does not allow automated filling, apply by hand and Mark as applied. |
| A web page containing instructions aimed at the AI | Page text is treated as data. The agent has no tools that reach outside the application tab, so the possible damage is small. |
| Wandering off | The agent's tools act only on the application tab. It cannot open arbitrary addresses. |
| Runaway cost or loops | Spending and turn caps on every run. |
| Uploading the wrong file | Uploads limited to `profile\` and the application's `drafts\`. |
| Not knowing what happened | Every action is written to the application's `events.log`. |

**Privacy.** All data stays in `job_app\` on your PC. The only things sent anywhere are what the agent needs for the task in front of it, sent to Claude: your resume and profile, the job posting, the fields on the current page, and the saved answers relevant to those fields, not the whole repository. Sign-in cookies live in `data\browser_profile\`; treat that folder like a password.

---

## 11. Build plan

### 11.1 Phases

Each phase ends with something you can use or check.

**Phase 0. Check your PC and prove the risky parts.** Small.
- `setup.bat`: finds Python, creates an isolated environment, installs packages, finds Chrome or Edge.
- Three short test scripts: (a) a Claude agent answers a one-line question using your chosen sign-in; (b) the dedicated browser opens, keeps its profile, fills and re-reads a field on a local test page; (c) an agent calls one of the app's tools that touches that page.
- Done when all three pass on your PC. This phase exists because I cannot run Windows programs from here, so the Windows-specific risks are tested first.

**Phase 1. Skeleton: data and dashboard.** Medium.
- File store, job state machine, settings, local server, live updates.
- Dashboard with the Jobs screen, Add a job by link (manual entry for now), Skip, Mark as applied, the Settings screen with both toggles.
- Done when you can add jobs, skip them, mark them applied, restart the app, and find everything as you left it. (R3 to R7, R13, R14 and R15 as stored settings.)

**Phase 2. Profile and requirements.** Medium.
- Resume upload and text extraction, profile builder, confirmation screen.
- Requirements boxes, requirements reader, "here is how I read that".
- Done when your resume and requirements are saved, and the structured reading matches what you meant. (R1, R2.)

**Phase 3. Job search.** Large.
- Tier 1 readers (Greenhouse, Lever, Ashby), watchlist, company address learning.
- Search agent for the open web, fit evaluator, duplicate removal, legitimacy and still-open checks, search log.
- Tier 2 readers, added one at a time.
- Done when Search for new jobs produces a ranked list where every listing passes your hard requirements, nothing skipped or applied reappears, and the log explains rejections. (R3, R17.)

**Phase 4. Form engine, no AI.** Large.
- Read fields, fill fields, verify, compare readings, the background watcher.
- Tested against local copies of the form patterns used by Greenhouse, Lever, Ashby and Workday, plus a practice application form built into the app.
- Two ways into a browser behind one interface: Claude's browser window (Playwright, as tested in Phase 0) and your own Chrome through the **Job App helper extension**. The helper is needed because Chrome 136 and later ignore outside-control switches on your main profile, and Chrome 137 and later no longer load extensions from the command line. You add it once from `chrome://extensions` (Developer mode, Load unpacked). It talks only to Job App on `127.0.0.1`, paired with a code the dashboard shows, and it does nothing until Job App asks.
- Done when the engine reads and fills every field type on the test forms and correctly reports which fields a person changed, in both browsers. Anything the helper cannot do in your own Chrome is listed and handled by pausing for you.
- **As built in checkpoint 4:** the engine and its practice forms, for Claude's browser window. The helper extension waits for your decision. See `docs/CHECKPOINTS.md`.

**Phase 5. Application agent and the learning loop.** Large. This is the core.
- The agent's tools and guardrails, the runner, Prepare and Continue, all the waiting statuses, the answer repository and matching, sensitive-question handling, pause after each page, the review summary.
- Done when, on the practice form and then on a real Greenhouse, Lever and Ashby posting of your choice: Prepare fills what it should, pauses where it should, Continue records what you typed, a second application reuses those answers, and nothing is ever submitted. (R8 to R12, R14, R16.)
- **As built in checkpoint 5:** all of it, for Claude's browser window, tested on the practice forms with a rule-based stand-in for the agent. The real agent is first run by `check.bat` on the PC; real postings are next. See `docs/CHECKPOINTS.md`.

**Phase 6. Cover letters and long answers.** Medium.
- Writing agent, drafts folder, AI-drafted marking, learning from your edits.
- Done when drafts use only real facts about you and you would send them with light edits. (R15.)
- **As built in checkpoint 6:** all of it, with code checks on every number and fact, and the cover letter as a Word file. Whether you would send the drafts is for you to judge on the PC.

**Phase 7. Harder sites and extras.** Open-ended, in whatever order you want.
- Workday and other multi-page systems.
- Several applications at once.
- Optional LinkedIn features (7.4), scheduled daily search, Answers page refinements.
- **As built in checkpoint 7:** multi-page forms and sign-in walls (Workday's needs your account), several applications at once, a daily usage cap, a daily search while Job App runs, and adding the page open in Claude's browser window ("you browse, the app captures" in 7.4; pages on LinkedIn and similar boards are added without being read). The opt-in LinkedIn features of 7.4 (searching while signed in as you, Easy Apply) were not built: 7.4 leaves them to your decision after using the rest, and they put your account at risk.

### 11.2 How it will be tested

**What I can test from here.** My workspace has Python, Playwright with a test browser, the web server packages, the Agent SDK and pytest. So I can run:
- unit tests for the store, state machine, duplicate removal and answer matching;
- the form engine against local copies of real form patterns;
- the dashboard in a test browser, with screenshots;
- the Prepare and Continue loop using a scripted stand-in for the agent.

**What only your PC can test.** Windows behaviour, your Claude sign-in, your installed browser, and real job sites. For these you run a script, and the results are written to `data\logs\`, which I can read through the connected folder. So the loop is: I write code into `job_app`, you run it, I read the logs and fix.

**A safe place to try it.** The app includes a practice application form. The whole Prepare, pause, Continue, review cycle can be exercised there before the app touches a real employer's site.

### 11.3 If helper agents are used to build it

If I split the build across helper agents to go faster, each one gets a single work package with: the files it owns; the formats it must follow (section 4, the tool table in 8.3, the statuses in 5.1); the tests that must pass; and an instruction not to change other packages. The natural packages are the folders under `app\`: core and server, dashboard, profile, search, browser and form engine, agents and answers.

---

## 12. Risks

| Risk | Likelihood | What the plan does about it |
|---|---|---|
| A site challenges or blocks automated filling | Medium | Uses your installed Chrome or Edge with a normal profile. On a challenge the app pauses for you. It does not try to defeat bot checks. Fallback is always apply by hand and Mark as applied. |
| Unusual form controls the engine cannot operate | High on Workday-style sites | Start with the simple systems. Per-site notes. Screenshot fallback. Pause for you rather than guess. |
| A wrong answer gets entered | Medium | Strict "blank beats wrong" rule, sensitive questions never guessed, every value shows its source, you review everything, corrections are learned. |
| Something gets submitted by accident | Low | No submit tool. Conservative button classification. Tested in Phase 5. |
| Windows-specific problems I cannot see from here | Medium | Phase 0 tests them first. Logs land in the shared folder. |
| Cost or plan limits | Medium | Caps per run and per day, simple fields filled without Claude, cost shown in the dashboard, cheaper models where they are enough. |
| Job board terms, LinkedIn account risk | Real if automated while signed in | Off by default. You browse, the app captures. Opt-in only, decided later. |
| Stale or duplicate postings | High | Still-open check, duplicate keys across sources. |
| The helper extension cannot do something in your own Chrome (an unusual upload control, a form inside a locked frame) | Medium | Claude's browser window stays the default. What the helper cannot do is paused for you, never guessed. The helper needs permission to read and change pages on every site, which is what autofill needs; this is stated before you add it. |
| Browser closed or PC restarted mid-application | Medium | Background saving of what you type. Refill from the record. |
| Billing or SDK rules change | Medium | Both sign-in methods supported. Link to Anthropic's article in the app. |
| Personal data in plain files | Inherent | Local only. Sensitive answers flagged. Never store IDs, bank details, passwords. `.gitignore` covers `data\`, `profile\` and `.env`. |

---

## 13. Assumptions and open questions

Assumptions I am building on unless you say otherwise:

1. One person uses the app, on this PC, for applications in English.
2. You always submit yourself. The app has no "submit for me" feature.
3. Sensitive questions are asked once and reused, never guessed (8.5).
4. One application is filled at a time until Phase 7.
5. Postings and applications open where the browser setting says. Claude's browser window is the default, as explained in section 1.
6. LinkedIn and job boards are handled as in 7.4.

Open questions, none of which block Phase 0 or 1:

1. Plan sign-in or API key (3.4)?
2. Do you want a spending cap lower or higher than the placeholders in 4.6?
3. Is there a watchlist of companies to start with?
4. For hard requirements the posting does not mention (often pay), list with a note, or hide? The plan defaults to list with a note.

---

## 14. Sources

- Claude Agent SDK overview: <https://code.claude.com/docs/en/agent-sdk>
- Running Claude Code programmatically: <https://code.claude.com/docs/en/headless>
- Using the Agent SDK with a Claude plan (billing): <https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan>
- Playwright: <https://playwright.dev/python/>
- Agent SDK interface details were read from the installed package, version 0.2.172.
- Public job-feed addresses for Greenhouse, Lever and Ashby are from prior knowledge and will be re-verified at the start of Phase 3.
