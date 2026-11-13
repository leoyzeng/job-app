# Checkpoints

The plan in `PLAN.md` is built in checkpoints. After each one, work stops for the user to try it and say whether to continue.

| # | Checkpoint | Plan phase | Status |
|---|---|---|---|
| 1 | Foundation: setup scripts, data files, dashboard | 0 and 1 | Done. The user ran it on their PC on 2026-11-11 (see below for what the logs show). |
| 2 | Profile and requirements: resume, hard requirements, soft preferences | 2 | Done. The user ran it on 2026-11-11 and the real agents worked (see below). The browser setting was added afterwards, at the user's request. |
| 3 | Job search | 3 | Built 2026-11-12. The user said to continue before trying it on the PC. |
| 4 | Form engine: read, fill and compare forms, no AI | 4 | Built 2026-11-12 for Claude's browser window. |
| 5 | Application agent and the learning loop: Prepare, Continue, review | 5 | Built 2026-11-12. |
| 6 | Cover letters and long answers | 6 | Built 2026-11-12. |
| 7 | Harder sites and extras | 7 | Built 2026-11-12. The user asked on 2026-11-12 to build checkpoints 5 to 7 without stopping. None of 3 to 7 has been run on the PC yet. The helper extension for the user's own Chrome still waits for the user's decision. |

## Decisions made since the plan was written

- **Sign-in:** agents use the Claude plan sign-in and its normal subscription limits (`auth.mode` is `subscription` in settings). An `ANTHROPIC_API_KEY` in the environment is ignored on purpose, because the runtime would otherwise prefer it and bill it.
- **Python:** 3.12.10 is installed on the user's PC. Chrome is installed.
- **Browser:** the user first wrote "for now, use the browser built in to claude code". A standalone program cannot drive the pane inside the Claude desktop app, so this was read as the dedicated browser window the app controls. On 2026-11-11 the user asked instead for a setting: open the posting either in "the claude code window" or in a window of the browser on their own computer. Built as `browser.mode` in settings:
  - `dedicated`, shown as **Claude's browser window** (the default): the dedicated window Job App starts and controls, with its own profile in `data\browser_profile`. Autofill runs here.
  - `own_chrome`, shown as **Your own Chrome**: a new tab in the user's everyday Chrome. For now Job App can only open pages there. Autofill there needs the helper extension planned for checkpoint 4, because Chrome 136 and later ignore remote-debugging switches on the default profile ([Chrome blog](https://developer.chrome.com/blog/remote-debugging-port)) and Chrome 137 and later no longer accept `--load-extension` ([Chromium extensions group](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY)). The helper is added once by hand (Developer mode, Load unpacked), needs permission to read and change pages on every site, and talks only to Job App on 127.0.0.1. The user decides at checkpoint 4 whether that trade is worth it.
- **Checkpoints:** the user asked for a check-in after each major feature.

## Checkpoint 1: what was built

- `setup.bat`, `run.bat`, `demo.bat`, `signin.bat`, `check.bat`.
- Data layer (`app/core`): job model and statuses, tracker file with atomic writes and a backup, duplicate detection, the full job lifecycle from the plan, settings.
- Local server (`app/server`): listens on 127.0.0.1 only, session cookie, refuses other hosts and other sites, live updates.
- Dashboard (`ui`): tabs, listings with the four buttons, Add a job by link, Settings, undo.
- PC check (`app/checks`): installed packages, browsers, Claude sign-in, the dedicated browser (fill every field type, attach the resume, keep data between runs), an agent reply, an agent using an app tool.
- Reusable pieces for later checkpoints: `app/browser/launch.py` (opens the dedicated browser), `app/agents/auth.py` (sign-in mode).

### Tested where

| What | Where it was tested |
|---|---|
| Data layer, server, dashboard (77 automated tests, including the dashboard driven in a headless browser) | Claude's Linux workspace |
| Browser check: fill, attach resume, persistence | Claude's Linux workspace, with Chrome, headless |
| Agent checks: the failure path when not signed in | Claude's Linux workspace |
| `.bat` scripts, the dashboard window, Chrome with a visible window, the agent checks succeeding | Not tested. Needs the user's PC. |

### Differences from the plan

- Package list is split into `requirements-core.txt` (dashboard) and `requirements-agents.txt` (browser and agents), so the dashboard still installs if the large agent package fails.
- Sample-data mode (`demo.bat`, folder `data_demo`) was added so every status can be seen before search and autofill exist.
- "Move back to New" always returns a job to New. From checkpoint 5 on, undoing a mistaken "Mark as applied" during review should return it to review while the form is still open.
- The dedicated browser keeps one profile folder per browser (`data\browser_profile\chrome`), because a profile written by one browser is not safe to open with another.

### What the user is asked to do

1. Run `setup.bat`, then `demo.bat` to look around, then `run.bat` and try adding, skipping and applying.
2. Run `signin.bat`, then `check.bat`.
3. Say what to change, and whether to go on to checkpoint 2.

Claude can read `data\logs\` through the connected folder, so results do not need to be pasted.

## Checkpoint 1: what the user's PC showed (2026-11-11, from `data\logs`)

- `setup.bat`, `run.bat`, `demo.bat` and `check.bat` ran. Windows 11, Python 3.12.10, Chrome found, dashboard opened as a Chrome app window.
- The dedicated browser test passed on Windows: Chrome opened, every field type was filled, the resume was attached, data persisted between runs.
- **The agents have not been proven on the user's PC.** The only full check on record (18:45) reported "Claude is not signed in" and skipped the agent tests. There is no later check, and at that time `signin.bat` left no log, so it is not known whether sign-in was completed afterwards. From checkpoint 2 on, `signin.bat` writes `data\logs\signin.log` and the dashboard shows the sign-in state.
- Installed versions were newer than the ones tested with: starlette 1.10.0, pydantic 2.14.3, playwright 1.65.0, claude-agent-sdk 0.2.180 (runtime 2.1.321), pypdf 7.0.1.

## Checkpoint 2: what was built

- **Resume reading** (`app/profile/resume.py`): PDF and Word, text plus the real addresses behind linked words, saved as `profile\resume.md`. Scans, password-protected and broken files are refused with a reason.
- **Agent runner** (`app/agents/runner.py`): the one place agents are started. No built-in tools, no Claude Code settings from the PC, turn and spend caps, plan sign-in enforced, errors turned into plain words (not signed in, usage limit, timeout), one line per run in `data\logs\agents.log`. Asks for a structured answer first and falls back to plain JSON.
- **Instruction files** (`prompts/`): `shared_rules.md`, `profile_builder.md`, `requirements_reader.md`. These are the final versions of sections 0, 5 and 6 of `AGENT_BRIEFS_DRAFT.md`.
- **Profile** (`app/profile/service.py`): the profile builder's answer is checked by code; any name, email, phone, link, school or employer that cannot be found in the resume text becomes a question for the user. Nothing is confirmed until the user saves.
- **Requirements**: saved in the user's words to `profile\requirements.md`. The reader agent adds how each hard requirement is checked, weights and search terms. Code rebuilds the hard and soft lists from the user's own lines, so the agent cannot add, drop, reword or move a requirement. A line whose wording does not fit its box becomes a question with two answers: hide jobs that miss it, or only rank them lower.
- **Dashboard**: a second screen, "Profile and requirements", opened first until everything is confirmed. Typing is kept in a draft so live updates never wipe it.
- **Sample-data mode** has its own made-up profile and stand-in agents.
- **check.bat** now also asks an agent for a structured answer, through the same runner.

### Tested where

| What | Where it was tested |
|---|---|
| 133 automated tests: resume reading, requirements, checks on agent output, the runner against the SDK's own message types, all routes, and both screens driven in a headless browser | Claude's Linux workspace, with stand-in agents |
| Resume text and link extraction on the user's sample resume | Claude's Linux workspace |
| Agents failing cleanly when not signed in | Claude's Linux workspace |
| A real agent reading a real resume or real requirements | **Not tested anywhere.** Needs the user's PC, signed in. |

### Known gaps

- The profile builder and requirements reader instructions have never been run against Claude. Expect to revise them after the first real run; `data\logs\agents.log` and the saved files show what came back.
- A scanned resume is refused rather than read by Claude directly (the plan's fallback). Not built.
- Saving the requirements again replaces the reading, including search terms the user edited by hand.
- The two readers use the `search` model setting (`sonnet`).

### What the user is asked to do

1. Run `signin.bat`, then `check.bat`, and confirm all lines say ok. This is the first time real agents run.
2. Run `run.bat`. On "Profile and requirements": click "Read my resume", correct and confirm the details, write requirements, answer any questions, click "Looks right".
3. Say what the agents got wrong, what to change, and whether to go on to checkpoint 3 (job search).

## Checkpoint 2: what the user's PC showed (2026-11-11, from `data\logs`)

- `signin.bat` completed: signed in with claude.ai, Pro plan.
- `check.bat` at 19:41: every line ok, including an agent reply (claude-sonnet-5-5, 0.9 s), a structured answer, and an agent reading a field in the dedicated browser through the app's own tool.
- Real agent runs, all ok on the first (structured) path: profile builder 30 s, about $0.08; requirements reader twice, 6 s each, about $0.02 and $0.01. `profile.json`, `requirements.md`, `search_profile.json` and `resume.md` were saved. No warnings or errors in `app.log`.

## After checkpoint 2: where postings open (version 0.2.1)

Asked for by the user on 2026-11-11.

- **Setting:** Settings has "Open job postings in", with Claude's browser window and Your own Chrome. Saved to `config\settings.json` as `browser.mode`. If Chrome is not installed, the second choice says so and links open in the default browser.
- **Opening a posting:** clicking the link on a listing asks Job App to open it (`POST /api/jobs/{id}/open`), which uses the setting. Ctrl-click and middle-click still do what the browser normally does. Opening a posting does not change the job's status.
- **Claude's browser window** (`app/browser/manager.py`): started on the first posting and kept open. The empty tab it starts with is used for the first posting; later postings get their own tabs. If the user closes the window, the next posting opens a new one. It closes when Job App stops. A page that fails to load still leaves its tab open, and the dashboard says it did not load.
- **If Claude's window cannot open**, the message offers "Open in your Chrome" for that one link, without changing the setting.
- Only `http` and `https` links are ever opened.

### Tested where

| What | Where it was tested |
|---|---|
| 159 automated tests, 26 of them new: link checks, both choices, the setting through the server, the dashboard's Settings dialog and link, and the failure offer | Claude's Linux workspace, with stand-in browsers |
| Claude's browser window for real: opens, reuses its first tab, adds tabs, reports a page that did not load, reopens after being closed, closes at the end | Claude's Linux workspace, headless test browser |
| A visible Chrome window on Windows, and a tab in the user's own Chrome | **Not tested.** Needs the user's PC. |

### What the user is asked to do

1. Close Job App's black window if it is open, then start `run.bat` again.
2. Add a job by link (any real posting), click its link: it opens in Claude's browser window.
3. In Settings choose Your own Chrome, click the link again: it opens as a tab in your Chrome.
4. Say whether to go on to checkpoint 3 (job search).

## Checkpoint 3: what was built (version 0.3.0)

The user said to continue on 2026-11-12. The browser setting from 0.2.1 had not been tried on the PC yet (no "Opened posting" lines in `app.log`).

- **Search pipeline** (`app/search/run.py`), as in PLAN.md 7.2. Each step is described below.
  - **Plan.** The search planner agent turns the confirmed requirements into web queries, title filters (kind words, role words, excluded words, years), countries, internship terms, which community lists to read, and up to 25 companies to look up. The plan is saved in `data/search_plan.json` and reused until the requirements change or 14 days pass. If the planner cannot be reached, a simple plan made by rules is used.
  - **Boards.** For watchlist companies and suggested companies, likely board names are tried on Greenhouse, Lever (US and EU hosts) and Ashby. Results are kept in `data/company_boards.json`, including "not found", which is re-checked after 30 days.
  - **Collect, all at once.** It reads every followed board (Workday boards are searched with the plan's kind words), the community lists (`SimplifyJobs/Summer<year>-Internships`, `New-Grad-Positions`; open listings for the plan's terms), and two web search agents. The agents have WebSearch plus the app's own `read_page` tool and nothing else.
  - **Screen.** It drops broken links, the same posting found twice, titles or places that cannot fit, anything already in the tracker in any status (R7), and postings judged before (`data/search_seen.json`). Rejections are remembered while the requirements are unchanged, closed postings for 60 days, and unreadable ones for 7.
  - **Judge, most promising first.** Order: watchlist companies, job-system postings, title fit, then how recent. Each posting is read in full, in order of preference: through its job system, through the JobPosting data the page embeds, then through a hidden browser for JavaScript pages (30 per search). Closed postings are dropped, and so are postings with two scam signs, or one sign plus an employer that cannot be confirmed. The fit evaluator judges 4 postings per run. Code matches its verdicts to the user's own requirement lines, and a "fail" without evidence becomes "unknown".
  - **Stopping.** Judging stops at "Most new jobs per search" (default 25), at twice that many postings judged, or at the usage cap (estimated $2). The next search continues with the rest.
  - **List.** It adds jobs that pass, with verdicts, evidence, soft score, concerns, notes on where they came from and whether the employer was confirmed. The full posting text goes to `data/postings/<id>.md`. A report goes to `data/search_runs/<time>.json`.
- **Safety of reading.** Every address is checked before reading and again on each redirect: only public web addresses, never this PC or the local network. This also applies to links found on web pages and to the hidden browser. LinkedIn, Indeed, Glassdoor, ZipRecruiter, Handshake and similar boards are never read (PLAN.md 7.4).
- **Add a job by link** now reads the posting (company, title, place, pay, description) and judges it in the background. Details the user types win over the page. A job added this way is listed even if it fails a requirement, with "Fails your requirements" shown.
- **Dashboard.**
  - "Search for new jobs" works.
  - A strip under the header shows progress with Stop, then the result.
  - A new **Search** screen shows the last search's report (counts, sources, why each judged posting was left out, what was screened out, the plan), the companies to watch, and the source settings: the three sources, strict mode, the jobs-per-search cap and the usage cap.
  - Listings show "Checking", "Fails your requirements" and "Could not verify" lines.
- **Instruction files:** `prompts/search_planner.md`, `prompts/search_agent.md`, `prompts/fit_evaluator.md`. Sections 3 and 4 of `AGENT_BRIEFS_DRAFT.md` are marked as superseded.
- **check.bat** adds three checks: the job feeds can be reached (and how many open listings the community list has), the hidden browser starts, and an agent can run one web search.
- **Sample-data mode** searches a made-up web: three companies, one on each job system, a community list and a company page, with stand-in agents. Nothing goes on the internet.

### Decisions made while building

- The community lists by Simplify and the Pitt Computer Science Club are the main Tier 2 source. For Summer 2027 the list is large, about 2,000 roles. It is public and meant to be read.
- Workday is read through the JSON service its own career pages use. It is not a published API, so it is read gently (at most 5 pages of 20 per board per search) and any failure is skipped.
- The usage cap is an estimate from Claude's own cost reporting. With the plan sign-in nothing is charged per dollar; the cap limits how much of the plan's usage one search can take. Each web agent is capped at an estimated $0.60.
- A posting that does not mention a hard requirement is listed with "could not verify" unless strict mode is on, as the plan says. The evaluator is told that a missing fact is "unknown", never "fail", even where a requirement's test wording says otherwise (the user's "3 month internship" test reads "if no duration is stated, it does not pass").

### Tested where

| What | Where it was tested |
|---|---|
| 259 automated tests, 100 of them new. They cover address safety (local addresses, redirects, names that point at the local network), page text and embedded posting data, every job system's links and readers, the community list, the filters, checks on every agent's answers, whole searches through the server (listing, rejection, closed postings, repeats, the second search, changed requirements, strict mode, budget, caps, sources off, a failing source, not signed in, stop), Add a job by link, and the dashboard in a headless browser | Claude's Linux workspace, with a made-up web and stand-in agents |
| The hidden browser: a JavaScript-drawn page read in full, and a page on this PC refused | Claude's Linux workspace, headless Chromium |
| Sample-data mode started as `demo.bat` starts it, with a search run through it | Claude's Linux workspace |
| The real Greenhouse, Lever, Ashby, Workday and GitHub feeds | **Not tested.** Neither Claude's workspace nor the PC's sandbox can reach them. The field names come from the published documentation. `check.bat` now tests them on the PC. |
| Real planner, web search agents and fit evaluator | **Not tested anywhere.** Needs the user's PC, signed in. |

### Known gaps

- Companies on other applicant systems (iCIMS, Oracle, SuccessFactors, Phenom, custom sites) cannot be found by name. Their postings come only through the community lists and the web search, and are read as pages.
- Workday boards cannot be found by a company's name, only learned from posting addresses.
- The instruction files for the three new agents have never run against Claude. Expect to revise them after the first real search; the report and `agents.log` show what came back.
- A board learned from Add a job by link while a search is running can be lost when the search saves its own copy of `company_boards.json`. It is learned again the next time.
- "Posted" dates on Workday are not read (Workday shows them only as "Posted 3 days ago").

### What the user is asked to do

1. Close Job App's black window, then run `check.bat`. Three new lines test the job feeds, the hidden browser and an agent's web search.
2. Run `run.bat`. Optionally, on the new **Search** screen, add companies to watch, one per line.
3. Click **Search for new jobs**. A real search takes a few minutes. Look at the listings and at the Search screen's report.
4. Optionally, add a job by pasting only its link.
5. Say what the search got wrong: jobs it should not have listed, jobs it missed, wrong verdicts. Also say whether to go on to checkpoint 4, the form engine, and whether you want autofill in your own Chrome badly enough to add a helper extension.

## Checkpoint 4: what was built (version 0.4.0)

The user said on 2026-11-12: "continue to the next checkpoint". They asked which models the agents use; the answer was Sonnet for every agent so far and for the application agent, Opus for long answers (`config\settings.json`, "models"). They did not answer the helper-extension question, so the engine is built for Claude's browser window, behind an interface a second backend can use later.

- **Form reader** (`app/forms/reader.js`, `read.py`). It runs inside every frame of the page and inside open shadow roots, and returns every field.
  - Each field has its label, kind, whether it is required, its options, its current value, any error message and its section.
  - **Kinds:** text, email, phone, URL, number, date, textarea and contenteditable; select and multiselect; radio and checkbox groups, including ARIA `role=radio` and `role=checkbox`; single checkboxes; file inputs, even when hidden; react-select style comboboxes, whose value is read from the element that shows the choice; dropdown buttons with listboxes (Workday); and Yes/No toggle buttons (Ashby).
  - **Labels** come from `aria-labelledby`, `<label for>`, wrapping labels (only the text before the field, so Lever's "ATTACH RESUME/CV" is not part of the label), `aria-label`, legends and nearby text.
  - It also reads buttons with their role (submit, next, back, add, upload, open application), headings, the step indicator, and signs of a CAPTCHA or a sign-in wall.
  - **Keys** come from the frame, kind, label and position, so they stay the same across redraws and reloads.
- **Rules** (`app/forms/classify.py`), enforced by the filler whoever calls it.
  - **Never filled:** passwords, payment details, government ID numbers.
  - **Left for you:** agreements, certifications, consent boxes and signatures.
  - **Marked sensitive** for the agent's rules: work authorization, sponsorship, salary, demographics, criminal record, clearance, age, citizenship.
  - **Certain:** name, email, phone, links, city, country, resume.
  - **Buttons:** a final submit is never clicked, nor is a button whose purpose is unclear.
- **Filler** (`app/forms/fill.py`).
  - Each kind of control is filled the way it needs. Typed fields send the events a person's typing does, which controlled React inputs need.
  - Dropdowns: it types to narrow the list, then if nothing fits looks through the full list. It matches by meaning ("Master's" to "Master's Degree", "Pittsburgh, PA" to "Pittsburgh, Pennsylvania, United States") and never guesses between two possible choices.
  - Dates are converted to the form's format, and files are attached only from allowed folders.
  - Every value is checked on a fresh reading afterwards, with a reason when it did not take. One field's trouble never stops the rest.
- **Compare** (`compare.py`) lists every field you filled, changed or cleared, and fields that appeared. **Watcher** (`watch.py`) re-reads a waiting form every few seconds, so typing is not lost if the window closes.
- **Certain fields** (`certain.py`) come from the confirmed profile, and fields the site already filled are kept. Only the first of two same-kind fields counts as certain (Lever's "Portfolio URL" but not "Other website").
- **Practice forms** (`ui/practice/`, served at `/practice/`): Greenhouse, Lever, Ashby and Workday styles, plus a company page with the form in a frame.
  - They copy each system's form structure, rebuilt from knowledge of these systems; real samples will come from `check.bat`.
  - They use controlled inputs that lose values set without proper events, react-select style menus that exist only while open, Workday's steps with validation errors, and a field inside a web component.
  - Their Submit buttons send nothing.
- **Dashboard:** Settings, then "Try it on a practice form". It opens the chosen form in Claude's browser window, fills the certain fields, and lists what was filled, what the site had already filled, and what was left and why. "Check what I changed" lists your changes.
- **check.bat** adds two checks. The engine fills every kind of field on all five practice forms in a visible Claude's browser window and finds a change made afterwards. The reader also reads one real application form each on Greenhouse, Lever and Ashby in a hidden browser, read only. It saves what it saw, and the page without scripts, to `data/logs/form_samples/`.

### Tested where

| What | Where it was tested |
|---|---|
| 321 automated tests, 62 of them new. The engine is tested in headless Chromium. Reading: all five practice forms, stable keys across a reload. Filling: every kind of field on every form, values surviving the page's redraws, choices that do not exist, files from other folders, fields never filled, custom ARIA controls. Changes: found on every kind of control. Steps: a multi-step form through validation errors to review, where Submit is refused. Also unclear buttons, certain fields from the profile, the watcher, option matching, classification of questions and buttons, the practice tryout through the server and the dashboard, and both new checks | Claude's Linux workspace |
| The engine in a visible Chrome window on Windows | **Not tested.** `check.bat` does it on the PC. |
| Real Greenhouse, Lever and Ashby forms | **Not tested.** Neither Claude's workspace nor the PC's sandbox can reach them. `check.bat` reads them on the PC and saves samples. |

### Known gaps

- The practice forms are reconstructions. Real forms will differ in details. The samples `check.bat` saves are the next input: Claude reads `data/logs/form_samples/` and fixes the reader before checkpoint 5 relies on it.
- Fields inside closed shadow roots, or in canvas-drawn forms, cannot be read.
- A combobox's options are known only while its menu is open. The reader reports them as unknown, and the filler opens the menu to choose.
- Workday's own forms need an account and are in Phase 7. The Workday-style practice form only tests the controls.
- Autofill in the user's own Chrome needs the helper extension. That is still the user's decision.

### What the user is asked to do

1. Close Job App's black window and run `check.bat`. Two new lines test the form engine on the practice forms (a window opens) and read real application forms (hidden, read only).
2. Run `run.bat`, open Settings, click "Try it on a practice form", choose one and click "Open and fill". In Claude's browser window, change a few fields, then click "Check what I changed".
3. Also, if not done yet, try the job search from checkpoint 3.
4. Say whether to go on to checkpoint 5 (the application agent: Prepare, Continue, the answer repository), and whether to add the helper extension for your own Chrome.


## Checkpoints 5, 6 and 7: what was built (version 0.7.0)

The user said on 2026-11-12: "continue with the rest of the checkpoints keep going until you finish the program". So 5, 6 and 7 were built one after another without a check-in. The helper-extension question was still unanswered, so autofill runs in Claude's browser window. With "Your own Chrome" chosen, postings still open there, Prepare uses Claude's window, and the listing says so.

### Checkpoint 5: the application agent and the learning loop

- **Prepare** (`app/apply/service.py`) opens the application link in a new tab of Claude's browser window, one tab per application. Then plain code does the certain parts:
  - it opens the form if the page is the job description;
  - it notices a closed posting;
  - it attaches the resume first and waits for the site to fill fields from it (marked "filled by the site");
  - it fills name, email, phone, links, city and country from the confirmed profile.
- **The application agent** (`app/apply/agent.py`, `prompts/application_agent.md`) then works for one *leg* through ten tools in `app/apply/tools.py`, and nothing else. It has no file, shell or web tools. The tools: get_page, fill_fields, upload_document, draft_long_answer, click, need_user, screenshot, inspect_element, save_site_note, end_leg.
  - Model: `models.autofill` (Sonnet). The spend for one application is capped by `budget_usd.per_application`.
  - `get_page` offers a **suggested** value for each field: what you typed earlier in this application, a certain profile value, your saved answer (exact, or "similar" with the saved wording for the agent to judge; a sensitive question only ever matches the same wording), or school, degree and discipline from the most recent education entry.
- **Rules enforced in the tools, whatever the agent does:**
  - **Fields that are yours.** Fields that are never filled, agreements and signatures are refused, and the field goes on the needs-you list. A sensitive question is refused unless the value is your own saved answer to that question, used exactly. A value you typed is never overwritten.
  - **Writing.** Long text must come from the writing agent.
  - **Buttons.** Submit and unclear buttons are refused. With "Pause after each page" on, Next is refused except once, after you approved the page.
  - **Secrets.** Screenshots black out password, payment and ID fields, and inspected HTML has their values removed.
  - **Finishing a page.** A leg cannot end "page done" or "ready for review" while a required field is empty and not on the needs-you list. It cannot end "ready for review" while a Next button exists.
- **Leg outcomes** move the listing:
  - needs input, or blocked by a sign-in or CAPTCHA: **Waiting for input**;
  - page done: **Waiting for page review**;
  - ready for review: **Waiting for review**;
  - failed: **Problem**, and what was filled is kept;
  - posting closed: **Posting closed**.
- **Continue** compares the form with how the agent left it (`snapshot.json`). If you moved to another page, it uses the watcher's last reading of the old page. Every change you made is then saved:
  - to `fields.json`, marked "entered by you";
  - to `data/answers.json`: created, updated, or "corrected" when it replaces the app's value, with the old answer kept in the answer's history.
  - Fields that are never filled are recorded only as "(entered by you)". Agreement and file fields are recorded but not learned.
  - Questions naming the company, or asking why you want this job, are kept as "written for one job". They are not reused, only kept as writing samples.
  - Then the next leg starts, with your changes in its brief. Sensitive values are shown to the agent only as "(answered)".
- **Continue on a finished form** saves your edits and checks the page for errors and empty required fields. It changes nothing and does not run the agent.
- **Mark as applied** saves your last changes the same way. A "similar question" answer you left unchanged gains the new wording.
- **After a leg**, a watcher re-reads the form every 4 seconds and saves your typing to `latest.json`. If the tab or Job App was closed, Continue reopens the form, takes your changes from `latest.json`, and puts back what was filled. When the page says the application was sent, the listing shows **Looks submitted** and makes Mark as applied the main button.
- **Restart:** an application that was being filled when Job App closed becomes a Problem saying so. Prepare carries on from the record.
- **Dashboard:**
  - Prepare and Continue work.
  - Show details lists the application: each field, its value and where it came from; review notes; drafts (the Word cover letter downloads); every step; and usage.
  - A new **Answers** screen to find, correct, mark sensitive, keep to one job, or delete answers.
- **Sample-data mode** (`demo.bat`) points the sample jobs at the practice forms and uses a rule-based stand-in for the agent, so Prepare can be tried without the plan.

### Checkpoint 6: cover letters and long answers

- **The writing agent** (`prompts/writing_agent.md`, `app/apply/writer.py`) runs on `models.writer` (Opus). It receives:
  - the question and its length limit (read from `maxlength` and from wording like "150 words");
  - the posting's full text;
  - `profile/resume.md` and the profile;
  - up to six samples of your writing: long answers you wrote or corrected, and any files in `profile/writing_samples/`.
- **Checks in code before anything is filled:**
  - every number must appear in the resume, profile, posting or question;
  - every fact the agent lists must be found in the resume or profile;
  - no placeholders such as "[Company]";
  - within the limit.
  - A draft that fails is sent back once with the reasons. If it fails again, or the agent answers "cannot answer", the field goes on the needs-you list with the reason.
- **Saving drafts.** Each draft is saved as `drafts/<question>.md` with the facts it used. A cover-letter upload field gets `drafts/cover_letter.docx`: a header from the profile, the date, the body, and a sign-off.
- **Marking drafts.** On the listing, drafted fields are marked **AI-drafted**, with a review note to read them before submitting.
- **When you edit a draft**, your version is saved beside it (`<question>.your_version.md`). It is also saved as an answer with source "you corrected the agent", so it becomes a writing sample.
- **When the setting is off**, long answers go on the needs-you list.

### Checkpoint 7: harder sites and extras

- **Multi-page forms:** the agent clicks Next only when the page is complete. With "Pause after each page" on, every page waits for your Continue. Tested on the three-step Workday-style practice form.
- **Sign-in walls and CAPTCHAs:** the agent fills nothing, and the listing says "Sign in or create your account on this site, then click Continue". Continue clears that item and carries on from the page you reached.
  - What you type into a password field is never read. The form reader reports only "(entered)" (`reader.js`).
  - Payment and ID fields are blanked the same way after classification (`read.py`).
  - So these values reach no reading, file, agent or answer. A test checks that the password typed during a sign-in appears in none of the files Job App wrote.
- **Several applications at once:** "Applications filled at the same time" (1 to 4) in Settings. Further Prepares queue, with "Waiting for another application to finish".
- **Daily usage cap** (`app/core/usage.py`). Today's estimated agent usage is added up from `agents.log` and shown in the top bar. When it reaches `budget_usd.per_day`, searches and Prepare/Continue are refused with the reason. Settings has the daily, per-application and per-search caps.
- **Daily search** (`app/search/schedule.py`). "Search for new jobs every day at" in Settings: once a day at or after that time, while Job App is running. It goes through the same checks as the button. The last result is shown in Settings.
- **"Use the page open in Claude's browser window"** in the Add dialog fills the link from the tab you were looking at.
- **check.bat** adds an autofill test. The real application agent fills the Greenhouse-style practice form for one leg, in Claude's browser window. It uses the confirmed profile, or the sample person if there is none, and starts with no saved answers. It should fill the profile fields, stop for the two sensitive questions, and never submit. What it did is saved in `data/logs/autofill_check/`. `--skip-autofill` leaves it out.

### Decisions made while building

- **Order of work.** The certain fields are filled by code before the agent starts, so the agent spends its turns on the questions that need judgement.
- **"Similar" saved answers** are offered to the real agent with both wordings. The rule-based stand-in never uses them.
- **Restoring after a reopen** happens in code, not by the agent: the values were already decided for this application.
- **Usage figures.** With a plan sign-in nothing is billed per run. The figures are the runtime's estimates, used to stop Job App from using up the plan's limits. Settings says so.
- **Continue on a finished form** does not run the agent. It only saves your edits and checks the page, which costs nothing.

### Tested where

| What | Where it was tested |
|---|---|
| 359 automated tests, 38 of them new. The application agent's tools and every refusal, on the practice forms in headless Chromium. Prepare, Continue, review and Mark as applied through the real server. Answers learned and reused in a second application. The Workday-style form walked page by page. A sign-in wall. A failed agent run and Prepare again. Skip closing the tab. The queue and two at once. Restart recovery. Drafts that fail the fact check. "Cannot answer". Edits to drafts kept. "Looks submitted". The usage cap, the daily search, the open-page link. The new dashboard parts. The check.bat autofill test's plumbing, with the stand-in agent | Claude's Linux workspace |
| The real application agent (Sonnet) and writing agent (Opus) through the Agent SDK | **Not tested.** Claude's workspace has no Claude sign-in. `check.bat` runs both on the PC on a practice form. |
| Autofill on real Greenhouse, Lever, Ashby and Workday applications | **Not tested.** Neither workspace can reach them. Start with one real application you would apply to anyway, and watch it. |
| Everything on Windows | **Not tested** since checkpoint 2. |

### Known gaps

- **The agents.** The real agents' behaviour is only known from their instructions and the rule-based stand-in's runs. The first `check.bat` autofill run on the PC is the first real evidence. Its log shows every tool call.
- **Real forms.** The practice forms are reconstructions. Real forms will need fixes to the reader and filler. `data/logs/form_samples/` (from check.bat) and each application's `events.log` are the inputs for those fixes.
- **Workday** needs an account per company. The agent stops at the sign-in page and waits for you. Account creation is always yours.
- **A reopened multi-page form** starts again at its first page, with the earlier values put back. The agent then works forward page by page.
- **Your own Chrome.** Autofill there still needs the helper extension. That is the user's decision.
- **The daily search** runs only while Job App is running. There is no Windows scheduled task.

### What the user is asked to do

1. Close Job App's black window and run `check.bat`. It now also lets the application agent fill a practice form (a few minutes, some plan usage). Send the result, or say what it showed.
2. Run `run.bat`, choose a job you would apply to anyway, and click **Prepare**. Watch Claude's browser window. Fill what the listing asks for, click **Continue**, read everything, and submit it yourself.
3. Look at **Show details** on that listing and at the **Answers** screen.
4. Say whether to add the helper extension, so autofill also works in your own Chrome.

## 0.8.0: ranking, company spread, never clicking Next, questions by meaning (2026-11-12)

The user asked for four changes after checkpoint 7.

- **Ranking: hard requirements > soft preferences > resume match.**
  - The fit evaluator now judges each soft preference as met, not met or unknown, with evidence, instead of giving one score. Code turns that into points with your weights (`soft_points`). The 0-100 "Match" shown is derived from the points.
  - With "Also rank by how well jobs fit your resume" on (Search screen, default on), the evaluator also gets your skills, positions and projects. It returns a level (strong, some, little, none), the skills of yours the posting names, and the closest position or project. Code keeps a skill only if it is in your profile and named in the posting as a word, then scores 0-100.
  - The order is strict, in `app/core/ranking.py`: fewer unverified hard requirements first, then more soft points, then resume score, then newest. Resume match only decides between jobs that tie on the first two.
  - Jobs listed before 0.8.0 have only a 0-100 score; they rank by it in bands of ten.
- **Companies spread out.**
  - New tab: within the same hard-requirement tier, a company comes again only after four other companies, as long as there are others to show. Each company's best job keeps its place.
  - A search judges postings from companies not already in your New list first (companies you watch are not held back), each part spread the same way.
- **"Never click Next or Continue"** (Settings, default off).
  - Enforced in the tools: Next, Continue, Save and continue, Review and Apply buttons are refused; the code step does not click Apply.
  - The agent fills the page and ends with "page done". The listing says to click Next yourself, then Continue.
  - On Continue, Job App notices you moved to another page, and the agent works on the page you are on.
- **Questions recognised by meaning** (Settings, default on).
  - New question matcher agent (`prompts/question_matcher.md`, `app/apply/questions.py`, model `models.search`, Sonnet; at most one short run per page).
  - It gets each question with its section, page headings, choices, the questions before and after it, the job's location and country, and your saved answers. It returns what the question asks, a topic, whether it is sensitive, the place it is about, and a match to a saved answer: same, derived, or none.
  - Code decides what is used:
    - a question becomes sensitive if the matcher says so, never the other way round;
    - a match must be one of your saved answers, of the same type, not one kept for a single job, recorded for the same topic and place;
    - on a choice question the value must be one of the choices, and if your saved answer is itself a choice it must be that one;
    - a text question only takes your saved answer as it is;
    - "derived" (worked out from a related answer, such as citizenship settling work authorization) only for work authorization, sponsorship, visa and citizenship, only on choice questions.
  - Every answer reused this way is listed on the listing to check. Left unchanged at Mark as applied, the new wording is added to the answer, so next time it matches exactly. New answers record their topic and place.
  - If the matcher fails, only exact wording is matched and the application carries on.
- **Tests:** 372 automated tests pass in Claude's workspace (13 new). Neither the evaluator's new output nor the question matcher has run with the real model; `check.bat` runs the matcher as part of its autofill test.
