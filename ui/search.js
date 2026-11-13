/* The "Search" screen: what the last search did, the companies you watch, and where the search looks.

   The report comes from /api/search/report and is fetched again whenever a
   search finishes. The watchlist box keeps what you type until you save. */
window.JobAppSearch = (() => {
  "use strict";

  let ctx = null;            // { h, api, refresh, toast, day, getState, openView }
  let report = null;
  let reportFor = null;      // id of the run the report belongs to
  let loading = false;
  let watchDraft = null;     // text in the watchlist box, null until first drawn
  let watchBase = null;
  let lastKey = null;

  const OUTCOMES = [
    ["rejected", "Did not meet a hard requirement"],
    ["suspicious", "Looked like a scam"],
    ["closed", "Posting closed"],
    ["unreadable", "Could not be read"],
    ["not_judged", "Could not be judged"],
  ];

  function step(title, ...children) {
    const { h } = ctx;
    return h("section", { class: "step" }, h("h2", { class: "step-title" }, title), ...children);
  }

  function minutes(start, end) {
    const a = new Date(start), b = new Date(end);
    if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return "";
    const seconds = Math.max(0, Math.round((b - a) / 1000));
    if (seconds < 90) return seconds === 1 ? "1 second" : `${seconds} seconds`;
    return `${Math.round(seconds / 60)} minutes`;
  }

  async function loadReport(force) {
    const search = ctx.getState().data.search;
    const id = search.running ? null : (search.last && search.last.id);
    if (!force && (loading || id === reportFor)) return;
    loading = true;
    const result = await ctx.api("GET", "/api/search/report");
    loading = false;
    if (result.ok) {
      report = result.data.report;
      reportFor = id;
      lastKey = null;
      draw();
    }
  }

  // ---------- last search ----------

  function tally(counts) {
    const { h } = ctx;
    const judged = ["listed", "rejected", "suspicious", "closed", "unreadable", "not_judged"]
      .reduce((sum, k) => sum + (counts[k] || 0), 0);
    const rows = [
      ["Postings found", counts.found || 0],
      ["Screened out by title or place", counts.filtered || 0],
      ["The same posting found twice", counts.repeat || 0],
      ["Already in your list (in any tab)", counts.already_listed || 0],
      ["Judged in an earlier search", counts.judged_before || 0],
      ["Read and judged this time", judged],
      ["Listed", counts.listed || 0],
    ];
    return h("dl", { class: "tally" }, rows.flatMap(([name, value]) => [
      h("dt", null, name), h("dd", { class: name === "Listed" ? "strong" : null }, String(value))]));
  }

  function sourcesBlock(sources) {
    const { h } = ctx;
    if (!sources.length) return null;
    return h("div", { class: "read-list" }, h("h4", null, "Where it looked"), h("ul", null, sources.map((s) =>
      h("li", null,
        h("b", null, s.name), s.count === 1 ? " 1 posting" : ` ${s.count} postings`,
        !s.ok && h("span", { class: "problem" }, " Failed."),
        s.detail && h("span", { class: "why" }, s.detail),
        (s.errors || []).length > 0 && h("span", { class: "why" }, `Could not read: ${s.errors.slice(0, 5).join("; ")}`)))));
  }

  function outcomesBlock(outcomes) {
    const { h } = ctx;
    const blocks = [];
    for (const [kind, title] of OUTCOMES) {
      const items = outcomes.filter((o) => o.outcome === kind);
      if (!items.length) continue;
      blocks.push(h("div", { class: "read-list", "data-outcome": kind },
        h("h4", null, `${title} (${items.length})`),
        h("ul", null, items.map((o) => h("li", null,
          /^https?:\/\//i.test(o.url || "")
            ? h("a", { href: o.url, target: "_blank", rel: "noopener noreferrer" }, o.label) : o.label,
          o.reason && h("span", { class: "why" }, o.reason))))));
    }
    return blocks;
  }

  function filteredBlock(filtered) {
    const { h } = ctx;
    if (!filtered.length) return null;
    const total = filtered.reduce((sum, f) => sum + f.count, 0);
    return h("details", { class: "fold" },
      h("summary", null, `Screened out by title or place (${total})`),
      h("ul", null, filtered.map((f) => h("li", null, h("b", null, `${f.reason[0].toUpperCase()}${f.reason.slice(1)} (${f.count})`),
        f.examples.length > 0 && h("span", { class: "why" }, `For example: ${f.examples.join("; ")}`)))));
  }

  function planBlock(plan, reused) {
    const { h } = ctx;
    if (!plan) return null;
    const rows = [
      ["Web searches", plan.queries.join("; ")],
      ["A title must name one of", plan.kind_words.join(", ")],
      ["and one of", plan.role_words.join(", ")],
      ["A title must not name", plan.exclude_words.join(", ")],
      ["Countries", plan.countries.join(", ")],
      ["Years", plan.years.join(", ")],
      ["Internship terms", plan.terms.join(", ")],
      ["Suggested companies", plan.companies.map((c) => c.name).join(", ")],
      ["Note", plan.notes],
    ].filter(([, value]) => value);
    return h("details", { class: "fold" },
      h("summary", null, reused ? "How the search was planned (same plan as before)" : "How the search was planned"),
      h("dl", { class: "plan" }, rows.flatMap(([name, value]) => [h("dt", null, name), h("dd", null, value)])));
  }

  function lastStep(data) {
    const { h, day } = ctx;
    const search = data.search;
    if (search.running) {
      const run = search.run;
      return step("Search running",
        h("p", { class: "job-status", "data-tone": "pen" },
          h("span", { class: "label" }, h("span", { class: "live", "aria-hidden": "true" }), "Searching"),
          h("span", { class: "detail" }, run.message)),
        tally(run.counts));
    }
    if (!report) {
      return step("Last search", h("p", { class: "step-lead" },
        search.last ? "Loading the report." : "No search yet. Click Search for new jobs at the top of the page."));
    }
    const tone = { done: "done", stopped: "muted", failed: "red" }[report.status] || "muted";
    const label = { done: "Finished", stopped: "Stopped", failed: "Failed" }[report.status] || report.status;
    const when = `${day(report.started_at)}, ${minutes(report.started_at, report.finished_at)}`;
    const parts = [
      h("p", { class: "job-status", "data-tone": tone }, h("span", { class: "label" }, label),
        h("span", { class: "detail" }, report.message.replace(/^Search (finished|stopped)\. /, ""))),
      h("p", { class: "step-note" }, `${when}. Estimated Claude usage: $${(report.cost_usd || 0).toFixed(2)}.`),
      tally(report.counts || {}),
      sourcesBlock(report.sources || []),
      ...outcomesBlock(report.outcomes || []),
      filteredBlock(report.filtered || []),
      (report.notes || []).length > 0 && h("div", { class: "read-list" }, h("h4", null, "Notes"),
        h("ul", null, report.notes.map((n) => h("li", null, n)))),
      planBlock(report.plan, report.plan_reused),
    ];
    return step("Last search", ...parts);
  }

  // ---------- watchlist ----------

  function watchStep(data) {
    const { h } = ctx;
    const saved = data.settings.search.watchlist.join("\n");
    if (watchDraft === null || watchBase !== saved) {
      watchDraft = saved;
      watchBase = saved;
    }
    const rows = data.watchlist.map((w) => {
      let text;
      if (w.boards.length) {
        text = w.boards.map((b) => {
          const read = b.last_error ? ` Last read failed: ${b.last_error}`
            : (b.last_count != null ? ` ${b.last_count} open jobs when last read.` : "");
          return `${b.system} board found.${read}`;
        }).join(" ");
      } else if (w.checked) {
        text = w.note || "No board found.";
      } else {
        text = "Not looked up yet. The next search will.";
      }
      return h("li", null, h("b", null, w.name), h("span", { class: "why" }, text));
    });
    return step("Companies to watch",
      h("p", { class: "step-lead" }, "Every search reads these companies' own job boards, on top of the companies the planner suggests. One company per line."),
      h("label", { class: "field" },
        h("span", { class: "field-label" }, "Companies"),
        h("textarea", {
          id: "watchlist", rows: "5", "data-bind": "watchlist", autocomplete: "off",
          oninput: (event) => { watchDraft = event.target.value; },
        }, watchDraft)),
      h("div", { class: "step-actions" }, h("button", {
        type: "button", class: "btn btn-main", id: "btn-save-watchlist",
        onclick: saveWatchlist,
      }, "Save companies")),
      rows.length > 0 && h("div", { class: "read-list" }, h("ul", null, rows)),
      h("p", { class: "step-note" }, "Job App finds a company's board on Greenhouse, Lever or Ashby by its name. "
        + "Companies on other systems are still covered by the web search and the community lists."));
  }

  async function saveWatchlist() {
    const names = [...new Set((watchDraft || "").split("\n").map((n) => n.trim()).filter(Boolean))];
    const result = await ctx.api("PUT", "/api/settings", { search: { watchlist: names } });
    if (result.ok) {
      ctx.toast(names.length === 1 ? "Saved 1 company." : `Saved ${names.length} companies.`);
      watchBase = null;
    } else {
      ctx.toast((result.data && result.data.message) || "The companies could not be saved.");
    }
    await ctx.refresh();
  }

  // ---------- sources ----------

  const SOURCES = [
    ["boards", "Company job boards",
     "Reads the job boards of the companies you watch and the companies the planner suggests, on Greenhouse, Lever, Ashby and Workday."],
    ["lists", "Community job lists",
     "Reads the internship and new-grad lists kept on GitHub by Simplify and the Pitt Computer Science Club."],
    ["web", "Web search agent",
     "Two agents search the web with queries made from your requirements and open the postings they find. Uses more Claude usage than the other sources."],
  ];

  function sourcesStep(data) {
    const { h } = ctx;
    const search = data.settings.search;
    const use = Object.assign({ boards: true, lists: true, web: true }, search.sources);
    const switchRow = (id, checked, title, help, onchange) => h("label", { class: "switch-row" },
      h("input", { type: "checkbox", role: "switch", id, checked, onchange }),
      h("span", { class: "switch", "aria-hidden": "true" }),
      h("span", { class: "switch-text" }, h("span", { class: "switch-title" }, title), h("span", { class: "switch-help" }, help)));
    const rows = SOURCES.map(([key, title, help]) => switchRow(`src-${key}`, use[key], title, help,
      (event) => save({ search: { sources: { [key]: event.target.checked } } }, event.target)));
    return step("Where to look",
      ...rows,
      switchRow("set-resume-match", search.resume_match !== false, "Also rank by how well jobs fit your resume",
        "Your skills, past positions and projects are compared with each posting. This only decides between jobs that do equally well on your hard requirements and soft preferences: one more preference met always ranks higher.",
        (event) => save({ search: { resume_match: event.target.checked } }, event.target)),
      switchRow("set-strict", search.strict_hard_requirements, "Hide jobs a hard requirement cannot be checked against",
        "When a posting does not say, for example, its pay or start date, it is normally listed with a \"could not verify\" note. Turn this on to hide those instead.",
        (event) => save({ search: { strict_hard_requirements: event.target.checked } }, event.target)),
      h("div", { class: "field-row narrow" },
        h("label", { class: "field" },
          h("span", { class: "field-label" }, "Most new jobs per search"),
          h("input", { type: "number", id: "set-max-new", min: "1", max: "200", value: String(search.max_new_per_search),
                       onchange: (event) => save({ search: { max_new_per_search: Number(event.target.value) } }) })),
        h("label", { class: "field" },
          h("span", { class: "field-label" }, "Usage cap per search (estimated $)"),
          h("input", { type: "number", id: "set-budget", min: "0", step: "0.5", value: String(data.settings.budget_usd.per_search),
                       onchange: (event) => save({ budget_usd: { per_search: Number(event.target.value) } }) }))),
      h("p", { class: "step-note" }, "The cap uses the cost each agent run reports: with your own API key that is what you pay, and with a Claude plan it limits how much of your plan one search can take. Judging stops when it is reached, and the next search carries on where it left off."));
  }

  async function save(patch, box) {
    const result = await ctx.api("PUT", "/api/settings", patch);
    if (!result.ok) {
      if (box) box.checked = !box.checked;
      const fields = (result.data && result.data.fields) || {};
      ctx.toast(Object.values(fields)[0] || (result.data && result.data.message) || "That setting could not be saved.");
    }
    await ctx.refresh();
  }

  // ---------- draw ----------

  function draw() {
    const data = ctx.getState().data;
    const root = document.querySelector("#search");
    if (!data) { root.replaceChildren(); return; }
    const search = data.search;
    if (!search.running && search.last && search.last.id !== reportFor) loadReport(false);
    const key = JSON.stringify([search, data.watchlist, data.settings.search, data.settings.budget_usd, reportFor,
                                watchBase === data.settings.search.watchlist.join("\n")]);
    if (key === lastKey && root.childElementCount) return;
    lastKey = key;
    const active = document.activeElement;
    const keepFocus = active && active.id && root.contains(active) ? active.id : null;
    const caret = keepFocus === "watchlist" ? [active.selectionStart, active.selectionEnd] : null;
    root.replaceChildren(lastStep(data), watchStep(data), sourcesStep(data));
    if (keepFocus) {
      const again = root.querySelector(`#${CSS.escape(keepFocus)}`);
      if (again) {
        again.focus({ preventScroll: true });
        if (caret) { try { again.setSelectionRange(caret[0], caret[1]); } catch { /* fine */ } }
      }
    }
  }

  return {
    init(context) { ctx = context; },
    draw,
    reload() { loadReport(true); },
  };
})();
