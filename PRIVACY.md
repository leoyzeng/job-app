# Privacy

Job App runs on your computer. The project has no server, collects no telemetry, and never receives anything about you.

## What stays on your computer

Everything Job App keeps about you lives in your data folder (see the README): your resume and profile, requirements, jobs, answers, applications, search history, settings, logs, and the sign-ins of Job App's browser window. Run `jobapp forget` (`./jobapp.sh forget` on macOS and Linux) to delete all of it.

## What is sent, and to whom

- **To Claude, under your own account.** When an agent works, it is sent what that task needs:
  - your resume and confirmed profile,
  - the job posting,
  - the fields on the application page in front of it,
  - and the saved answers that fit those fields.

  This goes to Anthropic through your Claude plan or API key, or to your own cloud provider if you set one up. Your agreement with them applies. Answers to sensitive questions are filled by code from your own saved answers. Passwords, payment details and ID numbers are never read from the page or sent.
- **To job sites.** Job App reads public job listings (for example the Greenhouse, Lever and Ashby job-board feeds) and opens application pages in its browser window, like you would. What you submit on those sites is up to you.
- **To GitHub, once a day, if allowed.** With Settings → "Tell me about new versions" on, Job App asks GitHub for the project's latest release. The request carries Job App's version and nothing about you.

## The Claude runtime's own files

The Claude Agent SDK installs a copy of the Claude runtime. It keeps:

- your Claude sign-in, if you chose that option, in its own settings (`~/.claude`, or the macOS Keychain);
- transcripts of agent runs under `~/.claude/projects/`.

Job App deletes the transcripts of its own runs when it starts and stops, unless you switch on "Keep agent transcripts" in Settings. `jobapp signin --sign-out` removes the stored API key and the runtime's sign-in.

## Bug reports

Settings → "Copy diagnostics" produces text for a bug report. It covers versions, your operating system, which settings are on, counts of jobs by status, and the names and error kinds of recent agent failures. It leaves out your resume, profile, answers, jobs, companies, links, page contents, keys, email addresses and folder names. Nothing is sent: you paste it yourself.
