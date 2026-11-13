/* Job App dashboard. Plain JavaScript, no build step.
   The server is the source of truth: after every change the page asks for the
   whole state again and redraws. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);

  /** Build an element. Text is always set as text, never as HTML. */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === false || value == null) continue;
      if (key === "class") el.className = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      el.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const TABS = [
    ["new", "New"],
    ["in_progress", "In progress"],
    ["waiting", "Waiting on you"],
    ["applied", "Applied"],
    ["hidden", "Skipped and closed"],
  ];

  const STATUS_NAMES = {
    new: "New", skipped: "Skipped", queued: "Queued", filling: "Autofilling",
    waiting_for_input: "Waiting for input", waiting_for_page_review: "Waiting for page review",
    waiting_for_review: "Waiting for review", applied: "Applied", closed: "Posting closed", error: "Problem",
  };

  const VIEWS = [["jobs", "Jobs"], ["profile", "Profile and requirements"], ["search", "Search"], ["answers", "Answers"]];

  const SOURCE_NAMES = {
    profile: "from your profile", resume: "your resume", saved_answer: "your saved answer",
    ai_draft: "AI-drafted", user: "entered by you", site_prefill: "filled by the site", inferred: "chosen by the agent",
  };

  const state = {
    data: null,
    view: remembered("jobapp.view", ""),   // "" until the first load decides
    tab: remembered("jobapp.tab", "new"),
    open: new Set(),      // rows showing their details
    busy: new Set(),      // rows with a request in flight
    arrived: null,        // id of a row to flash once
    offline: false,
    locked: false,
    searchSeen: null,     // id of the finished search whose result strip was dismissed
    wasSearching: false,
    apps: new Map(),      // job id -> what /api/jobs/{id}/application returned
    appsLoading: new Set(),
  };

  function remembered(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
  }
  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private window: nothing to do */ }
  }

  // ---------- talking to the server ----------

  async function api(method, path, body) {
    try {
      const response = await fetch(path, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      let data = null;
      try { data = await response.json(); } catch { /* no body */ }
      return { ok: response.ok, status: response.status, data };
    } catch {
      return { ok: false, status: 0, data: null };
    }
  }

  let refreshing = false;
  let refreshAgain = false;

  async function refresh() {
    if (refreshing) { refreshAgain = true; return; }
    refreshing = true;
    const result = await api("GET", "/api/state");
    refreshing = false;
    if (result.ok && !result.data.profile) {
      // This page is newer than the program still running behind it.
      state.failure = "Job App was updated. Stop it in its terminal window (Ctrl+C) and start it again.";
    } else if (result.ok) {
      const finished = state.wasSearching && !result.data.search.running;
      state.wasSearching = result.data.search.running;
      if (result.data.search.running) state.wasShownLast = true;
      state.data = result.data;
      state.offline = false;
      state.locked = false;
      state.failure = null;
      for (const id of state.open) {
        const job = result.data.jobs.find((j) => j.id === id);
        if (job && job.application && job.application.folder) loadApplication(id);
      }
      if (window.JobAppAnswers) window.JobAppAnswers.stateChanged();
      if (finished && result.data.search.last) {
        window.JobAppSearch.reload();
        if (state.view !== "search") {
          toast(result.data.search.last.message, { actionLabel: "See details", onAction: () => openView("search"), ms: 10000 });
        }
      }
    } else if (result.status === 401) {
      state.locked = true;
    } else if (result.status === 0) {
      state.offline = true;
    } else {
      state.failure = (result.data && result.data.message) || "The job list could not be loaded.";
    }
    render();
    if (refreshAgain) { refreshAgain = false; refresh(); }
  }

  function listen() {
    const source = new EventSource("/api/events");
    source.onopen = () => { if (state.offline) refresh(); };
    source.onmessage = () => refresh();
    source.onerror = () => {
      // The browser keeps retrying by itself. Find out whether the app has
      // stopped or this page has lost its connection to it.
      refresh();
    };
  }

  // ---------- formatting ----------

  const dayFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  const timeFormat = new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });

  function toDate(text) {
    if (!text) return null;
    const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    const date = dateOnly ? new Date(+dateOnly[1], +dateOnly[2] - 1, +dateOnly[3]) : new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const day = (text) => { const d = toDate(text); return d ? dayFormat.format(d) : ""; };
  const dayAndTime = (text) => { const d = toDate(text); return d ? timeFormat.format(d) : ""; };

  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return "posting"; }
  }

  function foundText(job) {
    const via = (job.source && job.source.found_via) || "";
    if (!via) return "";
    return via === "added by you" ? "Added by you" : `Found via ${via}`;
  }

  // ---------- one listing ----------

  function statusLine(job) {
    const detail = job.status_detail || {};
    const message = detail.message || "";
    switch (job.status) {
      case "queued":
        return { tone: "pen", label: "Queued", text: message || "Waiting for the browser to be free." };
      case "filling":
        return { tone: "pen", live: true, label: "Autofilling", text: message };
      case "waiting_for_input":
        return { tone: "mark", label: "Waiting for input", text: message, needs: detail.needs || [] };
      case "waiting_for_page_review":
        return { tone: "mark", label: "Waiting for page review",
                 text: message || "Look over this page in the browser, then click Continue." };
      case "waiting_for_review":
        return { tone: "mark", label: "Waiting for review",
                 text: message || "Review and submit in the browser, then mark it as applied." };
      case "applied":
        return { tone: "done", label: "Applied", text: day(job.status_since) };
      case "skipped":
        return { tone: "muted", label: "Skipped", text: day(job.status_since) };
      case "closed":
        return { tone: "muted", label: "Posting closed", text: message };
      case "error":
        return { tone: "red", label: "Problem", text: message || "Something went wrong. Prepare tries again." };
      default:
        return null;
    }
  }

  function primaryAction(job) {
    const wanted = {
      new: "prepare", error: "prepare",
      waiting_for_input: "continue", waiting_for_page_review: "continue",
      waiting_for_review: "applied",
    }[job.status];
    return wanted && job.actions[wanted].enabled ? wanted : null;
  }

  function actionButton(job, action, label, isPrimary) {
    const info = job.actions[action];
    const notBuilt = info.applicable && !info.enabled;
    return h("button", {
      type: "button",
      class: "btn" + (isPrimary ? " btn-main" : ""),
      "data-action": action,
      disabled: !info.applicable || state.busy.has(job.id),
      "aria-disabled": notBuilt ? "true" : null,
      title: notBuilt ? info.reason : null,
      onclick: () => act(job, action),
    }, label);
  }

  function jobRow(job) {
    const open = state.open.has(job.id);
    const facts = h("ul", { class: "job-facts" },
      h("li", { class: "company" }, job.company),
      job.location && h("li", null, job.location),
      job.details && job.details.pay && h("li", null, job.details.pay),
      job.posted_at && h("li", null, `Posted ${day(job.posted_at)}`),
      job.details && job.details.deadline && h("li", null, `Apply by ${day(job.details.deadline)}`),
      foundText(job) && h("li", null, foundText(job)),
      // Only ordinary web links are made clickable, whatever the tracker file holds.
      /^https?:\/\//i.test(job.url || "")
        ? h("li", null, h("a", {
            href: job.url, target: "_blank", rel: "noopener noreferrer", title: job.url,
            "data-action": "open",
            "aria-label": `Open the posting on ${hostOf(job.url)} in ${browserName()}`,
            onclick: (event) => openPosting(event, job),
          }, hostOf(job.url)))
        : h("li", null, job.url),
    );

    const main = h("div", { class: "job-main" }, h("h2", { class: "job-title" }, job.title), facts);

    const line = statusLine(job);
    if (line) {
      const status = h("div", { class: "job-status", "data-tone": line.tone },
        h("p", null,
          h("span", { class: "label" + (line.tone === "mark" ? " hl" : "") },
            line.live && h("span", { class: "live", "aria-hidden": "true" }), line.label),
          line.text && h("span", { class: "detail" }, line.text)),
      );
      if (line.needs && line.needs.length) {
        status.append(h("ul", { class: "needs" }, line.needs.map((need) =>
          h("li", null, need.label, need.reason && h("span", { class: "why" }, `  ${need.reason}`)))));
      }
      main.append(status);
    }

    const app0 = job.application || {};
    if (app0.looks_submitted && job.status !== "applied") {
      main.append(h("div", { class: "job-status submitted", "data-tone": "mark" },
        h("p", null, h("span", { class: "label hl" }, "Looks submitted"),
          h("span", { class: "detail" }, "The application page says it was sent. If you submitted it, mark it as applied."))));
    }

    const match0 = job.match || {};
    if (job.judging) {
      main.append(h("p", { class: "job-status", "data-tone": "pen" },
        h("span", { class: "label" }, h("span", { class: "live", "aria-hidden": "true" }), "Checking"),
        h("span", { class: "detail" }, "Judging this posting against your requirements.")));
    } else if ((match0.failed || []).length) {
      main.append(h("p", { class: "job-status", "data-tone": "red" },
        h("span", { class: "label" }, "Fails your requirements"),
        h("span", { class: "detail" }, match0.failed.join("; "))));
    }
    if ((match0.unverified || []).length && !(match0.failed || []).length) {
      main.append(h("p", { class: "job-note" }, h("b", null, "Could not verify"), " ", match0.unverified.join("; ")));
    }

    if (job.description_summary) main.append(h("p", { class: "job-desc" }, job.description_summary));

    const match = job.match || {};
    if (match.soft_score != null) {
      main.append(h("p", { class: "job-match" },
        h("span", { class: "score" }, `Match ${match.soft_score}`),
        (match.soft_hits || []).join(", "),
        (match.concerns || []).length > 0 &&
          h("span", { class: "concern" }, h("b", null, "Check"), match.concerns.join("; ")),
      ));
    }
    const resumeOn = !state.data.settings || !state.data.settings.search || state.data.settings.search.resume_match !== false;
    if (resumeOn && match.resume_level && match.resume_level !== "none") {
      const skills = match.resume_skills || [];
      main.append(h("p", { class: "job-resume" },
        h("span", { class: "resume-level", "data-level": match.resume_level },
          { strong: "Strong resume match", some: "Some resume match", little: "Little resume match" }[match.resume_level]
            || "Resume match"),
        skills.length > 0 && h("span", null, skills.join(", ")),
        match.resume_note && h("span", { class: "resume-note" }, match.resume_note)));
    }

    main.append(h("p", { class: "job-more" }, h("button", {
      type: "button", class: "linklike", "data-action": "details", "aria-expanded": String(open),
      onclick: () => { open ? state.open.delete(job.id) : state.open.add(job.id); render(); },
    }, open ? "Hide details" : "Show details")));

    let buttons;
    if (job.status === "applied") {
      buttons = [actionButton(job, "restore", "Move back to New", false)];
    } else if (job.group === "hidden") {
      buttons = [actionButton(job, "restore", "Move back to New", false),
                 actionButton(job, "applied", "Mark as applied", false)];
    } else {
      const primary = (job.application && job.application.looks_submitted && job.actions.applied.enabled)
        ? "applied" : primaryAction(job);
      buttons = [
        actionButton(job, "skip", "Skip", false),
        actionButton(job, "prepare", "Prepare", primary === "prepare"),
        actionButton(job, "continue", "Continue", primary === "continue"),
        actionButton(job, "applied", "Mark as applied", primary === "applied"),
      ];
    }

    const row = h("article", {
      class: "job" + (open ? " open" : "") + (state.arrived === job.id ? " arrived" : ""),
      "data-id": job.id, "data-status": job.status,
      "aria-label": `${job.title} at ${job.company}`,
    }, main, h("div", { class: "job-actions" }, buttons));

    if (open) row.append(...[].concat(detailsBlock(job)));
    return row;
  }

  function detailsBlock(job) {
    const blocks = [];
    const hard = (job.match && job.match.hard) || [];
    if (hard.length) {
      const words = { pass: "Meets", fail: "Fails", unknown: "Could not verify" };
      blocks.push(h("div", null, h("h3", null, "Your hard requirements"), h("ul", null, hard.map((item) =>
        h("li", null,
          h("span", { class: `verdict-${item.result}` }, words[item.result] || item.result), ": ",
          h("b", null, item.requirement), item.evidence ? ` (${item.evidence})` : "")))));
    }
    const facts = [];
    if (job.details && job.details.type) facts.push(["Type", job.details.type]);
    if (job.details && job.details.remote) facts.push(["Where", job.details.remote]);
    if (job.details && job.details.team) facts.push(["Team", job.details.team]);
    if (job.application && job.application.pages_total) {
      facts.push(["Application", `page ${job.application.page || 1} of ${job.application.pages_total}`]);
    }
    if (facts.length) {
      blocks.push(h("div", null, h("h3", null, "Details"),
        h("ul", null, facts.map(([name, value]) => h("li", null, h("b", null, name), ` ${value}`)))));
    }
    const who = { user: "you", agent: "the agent", app: "the app" };
    const history = [h("li", null, h("b", null, dayAndTime(job.found_at)), " Listed")];
    for (const entry of job.history || []) {
      history.push(h("li", null, h("b", null, dayAndTime(entry.at)), ` ${STATUS_NAMES[entry.to] || entry.to}`,
        who[entry.by] ? `, by ${who[entry.by]}` : ""));
    }
    blocks.push(h("div", null, h("h3", null, "History"), h("ul", null, history)));
    const extra = h("div", { class: "job-extra" }, blocks);
    if (job.application && job.application.folder) {
      if (!state.apps.has(job.id)) loadApplication(job.id);
      return [extra, applicationBlock(job, state.apps.get(job.id))];
    }
    return extra;
  }

  // ---------- an application's record ----------

  async function loadApplication(jobId) {
    if (state.appsLoading.has(jobId)) return;
    state.appsLoading.add(jobId);
    const result = await api("GET", `/api/jobs/${encodeURIComponent(jobId)}/application`);
    state.appsLoading.delete(jobId);
    if (result.ok && result.data.application) {
      state.apps.set(jobId, result.data.application);
      if (state.open.has(jobId) && state.view === "jobs") renderList();
    }
  }

  function showValue(value) {
    if (Array.isArray(value)) return value.join(", ");
    if (value === true) return "ticked";
    if (value === false || value === "" || value == null) return "empty";
    const text = String(value);
    return text.length > 160 ? text.slice(0, 157) + "..." : text;
  }

  function applicationBlock(job, app) {
    if (!app) return h("div", { class: "app-record" }, h("p", { class: "muted" }, "Loading the application record."));
    const st = app.state || {};
    const parts = [h("h3", null, "The application so far")];
    const notes = st.review_notes || [];
    if (notes.length) {
      parts.push(h("ul", { class: "review-notes" }, notes.map((note) => h("li", null, note))));
    }
    const pages = new Map();
    for (const field of app.fields || []) {
      if (!pages.has(field.page)) pages.set(field.page, []);
      pages.get(field.page).push(field);
    }
    for (const [page, fields] of [...pages.entries()].sort((a, b) => a[0] - b[0])) {
      const rows = fields.filter((f) => f.value !== "" && f.value != null && f.value !== false);
      if (!rows.length) continue;
      parts.push(h("table", { class: "app-fields" },
        pages.size > 1 && h("caption", null, `Page ${page}`),
        h("thead", null, h("tr", null, h("th", { scope: "col" }, "Field"), h("th", { scope: "col" }, "Value"),
          h("th", { scope: "col" }, "Where it came from"))),
        h("tbody", null, rows.map((f) => h("tr", { "data-source": f.source },
          h("th", { scope: "row" }, f.label),
          h("td", null, showValue(f.value), f.verified === false && h("span", { class: "problem" }, " did not take")),
          h("td", { class: "source" }, SOURCE_NAMES[f.source] || f.source || "",
            f.source === "saved_answer" && f.match === "similar" ? " (similar question)" : ""))))));
    }
    const drafts = app.drafts || [];
    if (drafts.length) {
      parts.push(h("h4", null, "Drafts"), h("ul", { class: "drafts" }, drafts.map((d) => d.text
        ? h("li", null, h("details", null, h("summary", null, d.name), h("pre", { class: "draft-text" }, d.text)))
        : h("li", null, h("a", { href: `/api/jobs/${encodeURIComponent(job.id)}/drafts/${encodeURIComponent(d.name)}` }, d.name)))));
    }
    if ((app.events || []).length) {
      parts.push(h("details", { class: "events" }, h("summary", null, "What happened, step by step"),
        h("ul", null, app.events.map((line) => h("li", null, line.replace(/^\S+T(\d\d:\d\d):\d\d\S*\s/, "$1  "))))));
    }
    const meta = [];
    if (st.cost_usd) meta.push(`Agent usage so far: $${Number(st.cost_usd).toFixed(2)}`);
    meta.push(`Saved in ${app.folder}`);
    parts.push(h("p", { class: "app-meta" }, meta.join(". ") + "."));
    return h("div", { class: "app-record" }, parts);
  }

  // ---------- the page ----------

  function sortFor(tab) {
    if (tab === "new") {
      return (a, b) => {
        // The server ranks New jobs: hard requirements, soft preferences, resume match, companies spread out.
        if (a.rank != null && b.rank != null) return a.rank - b.rank;
        const sa = a.match && a.match.soft_score, sb = b.match && b.match.soft_score;
        if ((sb ?? -1) !== (sa ?? -1)) return (sb ?? -1) - (sa ?? -1);
        return (b.found_at || "").localeCompare(a.found_at || "");
      };
    }
    return (a, b) => (toDate(b.status_since) || 0) - (toDate(a.status_since) || 0);
  }

  function emptyState(tab) {
    const text = {
      new: ["No new jobs", "Search for new jobs, or add one by pasting its link."],
      in_progress: ["Nothing in progress", "Jobs the agent is filling in show up here."],
      waiting: ["Nothing needs you", "Applications that are paused for your input or review show up here."],
      applied: ["No applications yet", "Jobs you mark as applied are kept here."],
      hidden: ["Nothing skipped or closed", "Skipped jobs are kept on file so they are never listed again."],
    }[tab];
    const searching = state.data && state.data.search.running;
    return h("div", { class: "empty" }, h("h2", null, text[0]), h("p", null, text[1]),
      tab === "new" && h("div", { class: "empty-actions" },
        h("button", { type: "button", class: "btn btn-main", onclick: startSearch, disabled: searching }, "Search for new jobs"),
        h("button", { type: "button", class: "btn", onclick: openAdd }, "Add a job by link")));
  }

  function renderNotices() {
    const box = $("#notices");
    const items = [];
    if (state.locked) {
      items.push(["bad", `This page is not connected to Job App. Start it with '${(state.data?.app?.commands || {}).run || "jobapp run"}' and use the window it opens.`]);
    } else if (state.offline) {
      items.push(["bad", `Job App is not running. Start it with '${(state.data?.app?.commands || {}).run || "jobapp run"}'. This page reconnects by itself.`]);
    } else if (state.failure) {
      items.push(["bad", state.failure]);
    }
    if (state.data) {
      if (state.data.app.demo) {
        items.push(["", "Sample data. Everything here is made up, nothing touches your real files, and the agents are stand-ins that do not use Claude."]);
      }
      for (const notice of state.data.notices || []) items.push(["", notice]);
    }
    box.replaceChildren(...items.map(([kind, text]) => h("p", { class: `notice ${kind}`.trim() }, text)));
  }

  function renderTabs() {
    const counts = (state.data && state.data.counts) || {};
    $("#tabs").replaceChildren(...TABS.map(([id, label]) => {
      const count = counts[id] || 0;
      const marked = id === "waiting" && count > 0;
      return h("button", {
        type: "button", class: "tab" + (id === "hidden" ? " tab-last" : ""),
        "aria-current": state.tab === id ? "true" : null, "data-tab": id,
        onclick: () => { state.tab = id; remember("jobapp.tab", id); render(); },
      }, label, h("span", { class: "count" + (marked ? " hl" : "") }, String(count)));
    }));
  }

  function focusKey() {
    const el = document.activeElement;
    const row = el && el.closest && el.closest(".job");
    return row && el.dataset.action ? [row.dataset.id, el.dataset.action] : null;
  }

  function renderList() {
    const list = $("#list");
    if (!state.data) {
      list.replaceChildren(h("div", { class: "empty" }, h("p", null, state.locked || state.offline ? "" : "Loading your jobs.")));
      return;
    }
    const focused = focusKey();
    const jobs = state.data.jobs.filter((job) => job.group === state.tab).sort(sortFor(state.tab));
    list.replaceChildren(...(jobs.length ? jobs.map(jobRow) : [emptyState(state.tab)]));
    state.arrived = null;
    if (focused) {
      const again = list.querySelector(`.job[data-id="${CSS.escape(focused[0])}"] [data-action="${focused[1]}"]`);
      if (again && !again.disabled) again.focus();
      else list.focus({ preventScroll: true });
    }
  }

  function renderTop() {
    const usage = $("#usage");
    const u = state.data && state.data.usage;
    if (u && (u.today_usd > 0 || u.reached)) {
      usage.hidden = false;
      usage.dataset.tone = u.reached ? "red" : "";
      usage.textContent = u.reached
        ? `Daily cap reached ($${u.cap_usd.toFixed(2)})`
        : `Today: about $${u.today_usd.toFixed(2)}${u.cap_usd > 0 ? ` of $${u.cap_usd.toFixed(2)}` : ""}`;
      usage.title = "Agent usage today, as estimated by the agents. Change the daily cap in Settings.";
    } else {
      usage.hidden = true;
    }
    const search = $("#btn-search");
    const running = Boolean(state.data && state.data.search.running);
    search.disabled = running;
    search.textContent = running ? "Searching" : "Search for new jobs";
    document.title = state.data && state.data.counts.waiting
      ? `(${state.data.counts.waiting}) Job App` : "Job App";
  }

  function renderViews() {
    if (state.data && !VIEWS.some(([id]) => id === state.view)) {
      // First visit: start where there is something to do.
      state.view = state.data.profile.ready ? "jobs" : "profile";
    }
    const view = state.view || "jobs";
    const todo = state.data && !state.data.profile.ready;
    $("#views").replaceChildren(...VIEWS.map(([id, label]) => h("button", {
      type: "button", class: "view-link", "data-view": id, "aria-current": view === id ? "true" : null,
      onclick: () => openView(id),
    }, label, id === "profile" && todo && h("span", { class: "hl todo" }, "to do"))));
    $("#view-jobs").hidden = view !== "jobs";
    $("#view-profile").hidden = view !== "profile";
    $("#view-search").hidden = view !== "search";
    $("#view-answers").hidden = view !== "answers";
    for (const el of document.querySelectorAll("[data-for-view]")) el.hidden = !el.dataset.forView.split(" ").includes(view);
    return view;
  }

  function render() {
    if (!TABS.some(([id]) => id === state.tab)) state.tab = "new";
    renderNotices();
    const view = renderViews();
    if (view === "jobs") {
      renderTabs();
      renderList();
    } else if (view === "profile") {
      window.JobAppProfile.draw();
    } else if (view === "answers") {
      window.JobAppAnswers.draw();
    } else {
      window.JobAppSearch.draw();
    }
    renderStrip(view);
    renderTop();
    if ($("#dlg-practice").open) renderPractice();
  }

  function openView(id) {
    state.view = id;
    remember("jobapp.view", id);
    render();
    window.scrollTo(0, 0);
  }

  /* The strip under the header: a running search, then its result until dismissed. */
  function renderStrip(view) {
    const strip = $("#search-strip");
    const search = state.data && state.data.search;
    if (!search) { strip.hidden = true; return; }
    if (search.running) {
      const counts = search.run.counts || {};
      const tally = [counts.found ? `${counts.found} found` : "", counts.listed ? `${counts.listed} listed so far` : ""]
        .filter(Boolean).join(", ");
      strip.hidden = false;
      strip.dataset.tone = "pen";
      strip.replaceChildren(
        h("p", null, h("span", { class: "label" }, h("span", { class: "live", "aria-hidden": "true" }), "Searching"),
          h("span", { class: "detail" }, search.run.message), tally && h("span", { class: "tally-inline" }, tally)),
        h("div", { class: "strip-actions" },
          view !== "search" && h("button", { type: "button", class: "linklike", onclick: () => openView("search") }, "Details"),
          h("button", { type: "button", class: "btn", id: "btn-stop-search", onclick: stopSearch }, "Stop")));
      return;
    }
    const last = search.last;
    if (!last || !state.wasShownLast || state.searchSeen === last.id || view === "search") { strip.hidden = true; return; }
    strip.hidden = false;
    strip.dataset.tone = { done: "done", failed: "red" }[last.status] || "muted";
    strip.replaceChildren(
      h("p", null, h("span", { class: "label" }, { done: "Search finished", stopped: "Search stopped", failed: "Search failed" }[last.status] || "Search"),
        h("span", { class: "detail" }, last.message.replace(/^Search (finished|stopped)\. /, ""))),
      h("div", { class: "strip-actions" },
        h("button", { type: "button", class: "linklike", onclick: () => openView("search") }, "See what was checked"),
        h("button", { type: "button", class: "btn btn-plain", onclick: () => { state.searchSeen = last.id; render(); } }, "Dismiss")));
  }

  async function startSearch() {
    const result = await api("POST", "/api/search");
    if (!result.ok) {
      toast((result.data && result.data.message) || "The search could not start. Check that Job App is still running.");
      return;
    }
    state.wasShownLast = true;
    state.wasSearching = true;
    await refresh();
  }

  async function stopSearch() {
    await api("POST", "/api/search/stop");
    await refresh();
  }

  // ---------- buttons ----------

  function toast(message, { actionLabel, onAction, ms = 7000 } = {}) {
    const item = h("div", { class: "toast" }, h("span", null, message));
    const remove = () => item.remove();
    if (actionLabel) {
      item.append(h("button", { type: "button", onclick: () => { remove(); onAction(); } }, actionLabel));
    }
    $("#toasts").append(item);
    setTimeout(remove, ms);
  }

  async function act(job, action) {
    const info = job.actions[action];
    if (!info.enabled) {
      if (info.reason) toast(info.reason);
      return;
    }
    if (state.busy.has(job.id)) return;
    state.busy.add(job.id);
    render();
    const result = await api("POST", `/api/jobs/${encodeURIComponent(job.id)}/${action}`);
    state.busy.delete(job.id);
    await refresh();
    if (!result.ok) {
      toast((result.data && result.data.message) || "That did not go through. Try again.");
      return;
    }
    const name = `${job.title} at ${job.company}`;
    const undo = { actionLabel: "Undo", onAction: () => act(result.data.job, "restore") };
    state.apps.delete(job.id);
    if (action === "prepare") toast(`Preparing ${name} in Job App's browser window. Nothing is submitted.`);
    else if (action === "continue") toast(`Saving your changes to ${name} and carrying on.`);
    else if (action === "skip") toast(`Skipped ${name}.`, undo);
    else if (action === "applied") toast(`Marked ${name} as applied.`, undo);
    else if (action === "restore") toast(`Moved ${name} back to New.`);
  }

  // ---------- opening a posting ----------

  function browserName(mode) {
    const chosen = mode || (state.data && state.data.settings.browser.mode) || "dedicated";
    return chosen === "own_chrome" ? "your Chrome" : "Job App's browser window";
  }

  async function openPosting(event, job, mode) {
    // Ctrl-click, middle-click and the like keep doing what the browser
    // normally does with a link.
    if (event && (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
    if (event) event.preventDefault();
    const result = await api("POST", `/api/jobs/${encodeURIComponent(job.id)}/open`, mode ? { in: mode } : {});
    if (result.ok) {
      toast(result.data.opened.message);
      return;
    }
    const data = result.data || {};
    if (data.where === "dedicated") {
      // Claude's window could not be opened. Offer the everyday browser for this one link.
      toast(data.message, {
        actionLabel: "Open in your Chrome", ms: 12000,
        onAction: () => openPosting(null, job, "own_chrome"),
      });
    } else if (result.status === 0) {
      toast("Job App is not running, so the posting was not opened.");
    } else {
      toast(data.message || "The posting could not be opened.");
    }
  }

  // ---------- add a job ----------

  function setFieldErrors(form, fields) {
    for (const input of form.querySelectorAll("input, textarea")) {
      const message = (fields && fields[input.name]) || "";
      const slot = form.querySelector(`.field-error[data-for="${input.name}"]`);
      if (slot) slot.textContent = message;
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  function openAdd() {
    const form = $("#form-add");
    form.reset();
    setFieldErrors(form, {});
    $("#add-error").hidden = true;
    $("#dlg-add").showModal();
    form.elements.url.focus();
  }

  async function submitAdd(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector("[type=submit]");
    const body = Object.fromEntries(new FormData(form).entries());
    submit.disabled = true;
    submit.textContent = "Reading the posting";
    const result = await api("POST", "/api/jobs", body);
    submit.disabled = false;
    submit.textContent = "Add job";
    const banner = $("#add-error");
    if (result.ok) {
      $("#dlg-add").close();
      const job = result.data.job;
      state.tab = "new";
      remember("jobapp.tab", "new");
      state.arrived = job.id;
      await refresh();
      const extra = result.data.read_note ? ` ${result.data.read_note}`
        : (result.data.judging ? " It is being checked against your requirements." : "");
      toast(`Added ${job.title} at ${job.company}.${extra}`);
      return;
    }
    const data = result.data || {};
    setFieldErrors(form, data.fields || {});
    const fieldProblem = data.fields && Object.keys(data.fields).length > 0;
    const generic = !data.message || data.message === "Some details need fixing.";
    banner.hidden = fieldProblem && generic;
    if (!banner.hidden) {
      banner.textContent = data.message || "The job could not be added. Check that Job App is still running.";
    }
    if (fieldProblem) {
      const first = form.querySelector("[aria-invalid=true]");
      if (first) first.focus();
    }
  }

  // ---------- settings ----------

  function openSettings() {
    if (!state.data) return;
    const { settings, app } = state.data;
    for (const box of document.querySelectorAll("#dlg-settings [data-setting]")) {
      box.checked = Boolean(settings[box.dataset.setting]);
    }
    for (const radio of document.querySelectorAll("#set-browser input[type=radio]")) {
      radio.checked = radio.value === settings.browser.mode;
    }
    const browserInfo = state.data.browser || {};
    $("#own-chrome-help").textContent = browserInfo.chrome_found === false
      ? "Chrome was not found on this computer, so postings open in your default browser. Autofill still runs in Job App's browser window."
      : "A new tab in the Chrome you use every day, with your bookmarks and sign-ins. Postings open there; autofill still runs in Job App's browser window.";
    for (const input of document.querySelectorAll("#dlg-settings [data-setting-path]")) {
      const value = input.dataset.settingPath.split(".").reduce((obj, key) => (obj == null ? obj : obj[key]), settings);
      input.value = value == null ? "" : String(value);
    }
    const schedule = state.data.schedule || {};
    $("#daily-note").textContent = schedule.last_day
      ? `Last daily search: ${day(schedule.last_day)}, ${schedule.last_result}.`
      : "Job App must be running. If you start it later in the day, the search runs then.";
    $("#settings-saved").textContent = "";
    $("#settings-note").textContent = app.not_built.autofill
      ? "Saved now. These take effect once autofill is built."
      : "These apply to every application.";
    const signIn = settings.auth.mode === "api_key"
      ? "Your own API key (billed to your Claude Console account)"
      : "Your own Claude account (your plan's normal limits)";
    const cmds = app.commands || {};
    $("#data-note").textContent = `Everything Job App keeps about you stays on this computer, in one folder. `
      + `To delete all of it, close Job App and run '${cmds.forget || "jobapp forget"}'.`;
    $("#diagnostics-note").textContent = "";
    const rows = [
      ["Agents sign in with", signIn],
      ["Change it with", cmds.signin || "jobapp signin"],
      ["Your data is in", app.data_folder],
      ["Version", app.version],
    ];
    $("#about").replaceChildren(...rows.flatMap(([name, value]) => [h("dt", null, name), h("dd", null, value)]));
    $("#dlg-settings").showModal();
  }

  async function saveSetting(event) {
    const box = event.currentTarget;
    const note = $("#settings-saved");
    const result = await api("PUT", "/api/settings", { [box.dataset.setting]: box.checked });
    if (result.ok) {
      state.data.settings = result.data.settings;
      note.textContent = "Saved.";
    } else {
      box.checked = !box.checked;
      note.textContent = "";
      toast((result.data && result.data.message) || "That setting could not be saved.");
    }
  }

  async function saveField(event) {
    const input = event.currentTarget;
    const note = $("#settings-saved");
    const kind = input.dataset.kind;
    let value = input.value;
    if (kind === "int") value = parseInt(value, 10);
    else if (kind === "number") value = value === "" ? 0 : Number(value);
    if ((kind === "int" || kind === "number") && Number.isNaN(value)) { note.textContent = "Enter a number."; return; }
    const patch = {};
    const keys = input.dataset.settingPath.split(".");
    keys.reduce((obj, key, i) => (obj[key] = i === keys.length - 1 ? value : {}), patch);
    const result = await api("PUT", "/api/settings", patch);
    if (result.ok) {
      state.data.settings = result.data.settings;
      note.textContent = "Saved.";
      input.removeAttribute("aria-invalid");
    } else {
      input.setAttribute("aria-invalid", "true");
      note.textContent = "";
      toast((result.data && result.data.message) || "That setting could not be saved.");
    }
  }

  async function useCurrentPage() {
    const result = await api("GET", "/api/browser/current");
    const input = $("#form-add").elements.url;
    if (result.ok) {
      input.value = result.data.url;
      input.focus();
      return;
    }
    toast((result.data && result.data.message) || "The page open in Job App's browser window could not be read.");
  }

  async function saveBrowser(event) {
    const radio = event.currentTarget;
    const note = $("#settings-saved");
    const before = state.data.settings.browser.mode;
    const result = await api("PUT", "/api/settings", { browser: { mode: radio.value } });
    if (result.ok) {
      state.data.settings = result.data.settings;
      note.textContent = `Saved. Postings now open in ${browserName(radio.value)}.`;
      render();
    } else {
      for (const other of document.querySelectorAll("#set-browser input[type=radio]")) {
        other.checked = other.value === before;
      }
      note.textContent = "";
      toast((result.data && result.data.message) || "That setting could not be saved.");
    }
  }

  // ---------- practice form ----------

  let practiceStyle = "greenhouse";

  function openPractice() {
    $("#dlg-settings").close();
    renderPractice();
    $("#dlg-practice").showModal();
  }

  function renderPractice() {
    const dialog = $("#dlg-practice");
    const practice = state.data && state.data.practice;
    if (!practice) return;
    const styles = $("#practice-styles");
    const legend = styles.querySelector("legend");
    styles.replaceChildren(legend, ...Object.entries(practice.styles).map(([id, label]) => h("label", { class: "choice-row compact" },
      h("input", { type: "radio", name: "practice-style", value: id, checked: id === practiceStyle,
                   onchange: () => { practiceStyle = id; } }),
      h("span", { class: "switch-title" }, label))));
    const working = practice.status === "working";
    $("#btn-practice-open").disabled = working;
    $("#btn-practice-check").disabled = practice.status !== "ready";
    const result = $("#practice-result");
    if (practice.status === "idle") { result.replaceChildren(); return; }
    const tone = { working: "pen", ready: "done", error: "red" }[practice.status];
    const label = { working: "Working", ready: practice.styles[practice.style] || "Ready", error: "Problem" }[practice.status];
    const parts = [h("p", { class: "job-status", "data-tone": tone },
      h("span", { class: "label" }, working && h("span", { class: "live", "aria-hidden": "true" }), label),
      h("span", { class: "detail" }, practice.message))];
    const list = (title, items, row, id) => items.length > 0 && h("div", { class: "read-list", id },
      h("h4", null, title), h("ul", null, items.map(row)));
    const show = (v) => Array.isArray(v) ? v.join(", ") : (v === true ? "ticked" : (v === false || v === "" || v == null ? "empty" : String(v)));
    parts.push(
      list("What you changed", practice.changes, (c) => h("li", null, h("b", null, c.label || "(no label)"),
        h("span", { class: "why" }, `${show(c.before)} \u2192 ${show(c.after)}`)), "practice-changes"),
      list("Filled from your profile", practice.filled, (f) => h("li", null, h("b", null, f.label),
        ` ${f.value}`, f.status !== "ok" && h("span", { class: "problem" }, ` ${f.message || f.status}`)), "practice-filled"),
      list("Already filled by the site", practice.kept, (f) => h("li", null, h("b", null, f.label), ` ${show(f.value)}`), "practice-kept"),
      list("Left for the application agent or for you", practice.left, (f) => h("li", null,
        h("b", null, f.label), f.required ? " (required)" : "", h("span", { class: "why" }, f.why)), "practice-left"),
    );
    result.replaceChildren(...parts.filter(Boolean));
    if (!dialog.open) return;
  }

  async function startPractice() {
    const result = await api("POST", "/api/practice", { style: practiceStyle });
    if (!result.ok) toast((result.data && result.data.message) || "The practice form could not be opened.");
    await refresh();
  }

  async function checkPractice() {
    const result = await api("POST", "/api/practice/check");
    if (!result.ok) toast((result.data && result.data.message) || "The practice form could not be read.");
    await refresh();
  }

  // ---------- start ----------

  $("#btn-add").addEventListener("click", openAdd);
  $("#btn-practice").addEventListener("click", openPractice);
  $("#btn-open-data").addEventListener("click", async () => {
    const result = await api("POST", "/api/data/open");
    $("#diagnostics-note").textContent = result.ok ? "Opened." : ((result.data && result.data.message) || "Could not open the folder.");
  });
  $("#btn-diagnostics").addEventListener("click", async () => {
    const note = $("#diagnostics-note");
    const result = await api("GET", "/api/diagnostics");
    if (!result.ok) { note.textContent = (result.data && result.data.message) || "Could not collect the diagnostics."; return; }
    const text = result.data.text;
    try {
      await navigator.clipboard.writeText(text);
      note.textContent = "Copied. Paste it into your bug report. It holds no resume, answers, jobs or page contents.";
    } catch (err) {
      note.replaceChildren(h("span", null, "Copy this into your bug report:"), h("pre", { class: "diagnostics" }, text));
    }
  });
  $("#btn-practice-open").addEventListener("click", startPractice);
  $("#btn-practice-check").addEventListener("click", checkPractice);
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-search").addEventListener("click", startSearch);
  $("#form-add").addEventListener("submit", submitAdd);
  for (const box of document.querySelectorAll("#dlg-settings [data-setting]")) {
    box.addEventListener("change", saveSetting);
  }
  for (const radio of document.querySelectorAll("#set-browser input[type=radio]")) {
    radio.addEventListener("change", saveBrowser);
  }
  for (const input of document.querySelectorAll("#dlg-settings [data-setting-path]")) {
    input.addEventListener("change", saveField);
  }
  $("#btn-current-page").addEventListener("click", useCurrentPage);
  for (const button of document.querySelectorAll("dialog [data-close]")) {
    button.addEventListener("click", () => button.closest("dialog").close());
  }
  for (const dialog of document.querySelectorAll("dialog")) {
    // A click on the dimmed area outside the box closes it.
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  }

  window.JobAppProfile.init({ h, api, refresh, toast, day, getState: () => state });
  window.JobAppSearch.init({ h, api, refresh, toast, day, getState: () => state, openView });
  window.JobAppAnswers.init({ h, api, refresh, toast, day, getState: () => state });

  render();
  refresh().then(listen);
})();
