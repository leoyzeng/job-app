/* The "Profile and requirements" screen.

   What you type lives in a draft kept in this file, so a live update from the
   server never wipes a half-typed field. The draft is replaced only when the
   server's copy really changes: a resume was read, or you saved. */
window.JobAppProfile = (() => {
  "use strict";

  let ctx = null;            // { h, api, refresh, toast, day, getState }
  let lastKey = null;        // what was last drawn, to skip needless redraws
  let rev = 0;               // bumped when the draft's structure changes

  const draft = {
    facts: null, factsBase: null,
    hard: "", soft: "", linesBase: null,
    reading: null, readingBase: null,
    manual: false,           // "Fill it in myself" was chosen
  };

  // ---------- drafts ----------

  const EMPTY_FACTS = () => ({
    name: { first: "", last: "", full: "" },
    contact: { email: "", phone: "", city: "", state: "", country: "" },
    links: { linkedin: "", github: "", website: "" },
    education: [], experience: [], projects: [], skills: "", certifications: "",
  });
  const NEW_ENTRY = {
    education: () => ({ school: "", degree: "", field: "", start: "", end: "", end_is_expected: false, gpa: "", honors: "" }),
    experience: () => ({ employer: "", title: "", location: "", start: "", end: "", current: false, highlights: "" }),
    projects: () => ({ name: "", summary: "", tools: "" }),
  };

  function factsDraft(facts) {
    const copy = JSON.parse(JSON.stringify(facts));
    for (const item of copy.education) item.honors = item.honors.join("\n");
    for (const item of copy.experience) item.highlights = item.highlights.join("\n");
    for (const item of copy.projects) item.tools = item.tools.join(", ");
    copy.skills = copy.skills.join(", ");
    copy.certifications = copy.certifications.join("\n");
    return copy;
  }

  function readingDraft(reading) {
    return {
      titles: reading.titles.join(", "),
      keywords: reading.keywords.join(", "),
      locations: reading.locations.join("\n"),
      types: reading.types.join(", "),
      pay_floor: reading.pay_floor || "",
      remote_ok: reading.remote_ok === null ? "" : String(reading.remote_ok),
      weights: Object.fromEntries(reading.soft.map((item) => [item.text, item.weight])),
    };
  }

  function syncDrafts(view) {
    const factsBase = `${view.read_at}|${view.confirmed_at}|${view.facts ? 1 : 0}`;
    if (draft.factsBase !== factsBase) {
      draft.factsBase = factsBase;
      draft.facts = view.facts ? factsDraft(view.facts) : (draft.manual ? draft.facts : null);
    }
    const linesBase = JSON.stringify(view.requirements);
    if (draft.linesBase !== linesBase) {
      draft.linesBase = linesBase;
      draft.hard = view.requirements.hard.join("\n");
      draft.soft = view.requirements.soft.join("\n");
    }
    const readingBase = `${view.reading_at}|${view.reading_confirmed}|${view.reading ? 1 : 0}|${linesBase}`;
    if (draft.readingBase !== readingBase) {
      draft.readingBase = readingBase;
      draft.reading = view.reading ? readingDraft(view.reading) : null;
    }
  }

  // ---------- small builders ----------

  function field(obj, key, label, bind, opts = {}) {
    const { h } = ctx;
    const common = {
      "data-bind": bind, autocomplete: "off", placeholder: opts.placeholder || null,
      oninput: (event) => { obj[key] = event.target.value; },
    };
    const control = opts.rows
      ? h("textarea", { ...common, rows: opts.rows }, obj[key] || "")
      : h("input", { ...common, type: opts.type || "text", value: obj[key] || "" });
    return h("label", { class: "field" + (opts.wide ? " wide" : "") },
      h("span", { class: "field-label" }, label, opts.hint && h("span", { class: "optional" }, opts.hint)),
      control);
  }

  function tick(obj, key, label, bind) {
    const { h } = ctx;
    return h("label", { class: "tick" },
      h("input", { type: "checkbox", "data-bind": bind, checked: obj[key] ? true : null,
                   onchange: (event) => { obj[key] = event.target.checked; } }),
      label);
  }

  function statusLine(tone, label, text, live) {
    const { h } = ctx;
    return h("p", { class: "job-status", "data-tone": tone },
      h("span", { class: "label" + (tone === "mark" ? " hl" : "") },
        live && h("span", { class: "live", "aria-hidden": "true" }), label),
      text && h("span", { class: "detail" }, text));
  }

  function step(number, title, ...children) {
    const { h } = ctx;
    return h("section", { class: "step", "data-step": String(number) },
      h("h2", { class: "step-title" }, h("span", { class: "step-number" }, String(number)), title),
      ...children);
  }

  async function send(method, path, body, raw) {
    const result = raw ? await raw() : await ctx.api(method, path, body);
    if (!result.ok) {
      ctx.toast((result.data && result.data.message) || "That did not go through. Check that Job App is still running.");
    }
    await ctx.refresh();
    return result;
  }

  // ---------- 1. resume ----------

  function resumeStep(view, agents) {
    const { h, day } = ctx;
    const picker = h("input", { type: "file", accept: ".pdf,.docx", hidden: true, id: "resume-file" });
    picker.addEventListener("change", async () => {
      const file = picker.files[0];
      if (!file) return;
      if (view.confirmed && !window.confirm(
        "Reading a new resume replaces the details below, including your edits. Continue?")) {
        picker.value = "";
        return;
      }
      await send(null, null, null, async () => {
        try {
          const response = await fetch(`/api/profile/resume?name=${encodeURIComponent(file.name)}`, {
            method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file,
          });
          let data = null;
          try { data = await response.json(); } catch { /* no body */ }
          return { ok: response.ok, status: response.status, data };
        } catch { return { ok: false, status: 0, data: null }; }
      });
    });

    const busy = view.busy.profile;
    const parts = [picker];
    if (view.resume) {
      const size = view.resume.bytes < 1024 * 1024
        ? `${Math.max(1, Math.round(view.resume.bytes / 1024))} KB`
        : `${(view.resume.bytes / 1024 / 1024).toFixed(1)} MB`;
      parts.push(h("p", { class: "step-lead" },
        h("b", null, view.resume.file), `  ${size}, saved ${day(view.resume.modified)}`));
    } else {
      parts.push(h("p", { class: "step-lead" },
        "No resume yet. Upload a PDF or Word file and the app reads your details from it."));
    }
    if (busy) parts.push(statusLine("pen", "Reading your resume", "This usually takes under a minute.", true));
    else if (view.errors.profile) parts.push(statusLine("red", "Problem", view.errors.profile));
    else if (view.errors.resume) parts.push(statusLine("red", "Problem", view.errors.resume));
    else if (view.stale) parts.push(statusLine("mark", "Your resume file has changed", "Read it again to update your details."));
    else if (view.resume && !view.facts) parts.push(statusLine("mark", "Not read yet", "Read it to fill in your details below."));

    parts.push(h("div", { class: "step-actions" },
      h("button", { type: "button", class: "btn" + (view.resume ? "" : " btn-main"), disabled: busy,
                    onclick: () => picker.click() }, view.resume ? "Upload a different file" : "Upload resume"),
      view.resume && h("button", {
        type: "button", class: "btn" + (!view.facts || view.stale ? " btn-main" : ""), disabled: busy, id: "btn-read-resume",
        onclick: async () => {
          if (view.confirmed && !window.confirm(
            "Reading the resume again replaces the details below, including your edits. Continue?")) return;
          await send("POST", "/api/profile/read");
        },
      }, view.facts ? "Read it again" : "Read my resume")));
    return step(1, "Your resume", ...parts);
  }

  // ---------- 2. about you ----------

  function entries(kind, title, addLabel, build) {
    const { h } = ctx;
    const list = draft.facts[kind];
    return h("div", { class: "group" },
      h("h3", { class: "group-title" }, title),
      list.map((item, index) => h("div", { class: "entry", "data-entry": `${kind}-${index}` },
        build(item, `${kind}.${index}`),
        h("button", { type: "button", class: "linklike entry-remove",
                      onclick: () => { list.splice(index, 1); rev += 1; draw(); } }, "Remove"))),
      h("button", { type: "button", class: "btn", "data-add": kind,
                    onclick: () => { list.push(NEW_ENTRY[kind]()); rev += 1; draw(); } }, addLabel));
  }

  function aboutStep(view) {
    const { h, day } = ctx;
    if (!draft.facts) {
      return step(2, "About you",
        h("p", { class: "step-lead" }, "Nothing here yet. Read a resume above, or type your details yourself."),
        h("div", { class: "step-actions" }, h("button", {
          type: "button", class: "btn", id: "btn-manual",
          onclick: () => { draft.manual = true; draft.facts = EMPTY_FACTS(); rev += 1; draw(); },
        }, "Fill it in myself")));
    }
    const f = draft.facts;
    const parts = [];
    if (view.confirmed) parts.push(statusLine("done", "Confirmed", day(view.confirmed_at)));
    else parts.push(statusLine("mark", "Waiting for you to confirm", "Fix anything that is wrong, then save."));
    if (view.to_confirm.length) {
      parts.push(h("ul", { class: "needs questions" }, view.to_confirm.map((item) => h("li", null, item.question))));
    }
    parts.push(h("p", { class: "step-note" },
      "Applications are filled in from these details, so a mistake here is repeated on every form."));

    parts.push(h("div", { class: "group" }, h("h3", { class: "group-title" }, "Name and contact"),
      h("div", { class: "grid" },
        field(f.name, "first", "First name", "name.first"),
        field(f.name, "last", "Last name", "name.last"),
        field(f.name, "full", "Name as written on your resume", "name.full"),
        field(f.contact, "email", "Email", "contact.email", { type: "email" }),
        field(f.contact, "phone", "Phone", "contact.phone"),
        field(f.contact, "city", "City", "contact.city"),
        field(f.contact, "state", "State or province", "contact.state"),
        field(f.contact, "country", "Country", "contact.country"),
        field(f.links, "linkedin", "LinkedIn", "links.linkedin"),
        field(f.links, "github", "GitHub", "links.github"),
        field(f.links, "website", "Website or portfolio", "links.website"))));

    parts.push(entries("education", "Education", "Add a school", (item, bind) => h("div", { class: "grid" },
      field(item, "school", "School", `${bind}.school`),
      field(item, "degree", "Degree", `${bind}.degree`),
      field(item, "field", "Field of study", `${bind}.field`),
      field(item, "start", "Start", `${bind}.start`),
      field(item, "end", "End", `${bind}.end`),
      field(item, "gpa", "GPA", `${bind}.gpa`),
      tick(item, "end_is_expected", "The end date is an expected graduation date", `${bind}.end_is_expected`),
      field(item, "honors", "Honors and awards", `${bind}.honors`, { rows: 2, wide: true, hint: "one per line" }))));

    parts.push(entries("experience", "Experience", "Add a job", (item, bind) => h("div", { class: "grid" },
      field(item, "employer", "Employer", `${bind}.employer`),
      field(item, "title", "Job title", `${bind}.title`),
      field(item, "location", "Location", `${bind}.location`),
      field(item, "start", "Start", `${bind}.start`),
      field(item, "end", "End", `${bind}.end`),
      tick(item, "current", "I work here now", `${bind}.current`),
      field(item, "highlights", "What you did", `${bind}.highlights`, { rows: 4, wide: true, hint: "one point per line" }))));

    parts.push(entries("projects", "Projects", "Add a project", (item, bind) => h("div", { class: "grid" },
      field(item, "name", "Project", `${bind}.name`),
      field(item, "tools", "Tools used", `${bind}.tools`, { hint: "separated by commas" }),
      field(item, "summary", "Summary", `${bind}.summary`, { rows: 2, wide: true }))));

    parts.push(h("div", { class: "group" }, h("h3", { class: "group-title" }, "Skills and certifications"),
      h("div", { class: "grid" },
        field(f, "skills", "Skills", "skills", { rows: 3, wide: true, hint: "separated by commas" }),
        field(f, "certifications", "Certifications", "certifications", { rows: 2, wide: true, hint: "one per line" }))));

    parts.push(h("div", { class: "step-actions" }, h("button", {
      type: "button", class: "btn btn-main", id: "btn-save-facts",
      onclick: async () => {
        const result = await send("PUT", "/api/profile/facts", draft.facts);
        if (result.ok) ctx.toast("Your details are saved and confirmed.");
      },
    }, "Save and confirm")));
    return step(2, "About you", ...parts);
  }

  // ---------- 3. requirements ----------

  function readingPanel(view) {
    const { h, day } = ctx;
    const reading = view.reading;
    const d = draft.reading;
    const busy = view.busy.requirements;
    const parts = [h("h3", { class: "group-title" }, "Here is how I read that")];

    if (view.reading_stale) {
      parts.push(statusLine("mark", "Your requirements changed since this was read", "Save them above to read them again."));
    } else if (view.reading_confirmed) {
      parts.push(statusLine("done", "Confirmed", ""));
    } else {
      parts.push(statusLine("mark", "Waiting for you to confirm", "Correct anything below, then click Looks right."));
    }

    for (const item of reading.ambiguous) {
      parts.push(h("div", { class: "question", "data-question": item.text },
        h("p", null, h("span", { class: "hl" }, "Question"), " ", h("b", null, item.text)),
        h("p", { class: "question-text" }, item.question),
        h("div", { class: "step-actions" },
          h("button", { type: "button", class: "btn", disabled: busy, "data-box": "hard",
                        onclick: () => send("POST", "/api/requirements/settle", { text: item.text, box: "hard" }) },
            "Hide jobs that miss it"),
          h("button", { type: "button", class: "btn", disabled: busy, "data-box": "soft",
                        onclick: () => send("POST", "/api/requirements/settle", { text: item.text, box: "soft" }) },
            "Only rank them lower"))));
    }

    if (reading.hard.length) {
      parts.push(h("div", { class: "read-list" }, h("h4", null, "A job is listed only if"),
        h("ul", null, reading.hard.map((item) => h("li", null, h("b", null, item.text),
          h("span", { class: "why" }, item.test ? `Checked by: ${item.test}` : "No check written yet. Read again to add one."))))));
    }
    if (reading.soft.length) {
      parts.push(h("div", { class: "read-list" }, h("h4", null, "Jobs rank higher if"),
        h("ul", null, reading.soft.map((item, index) => {
          const select = h("select", {
            "data-bind": `weight.${index}`, "aria-label": `How much "${item.text}" matters`,
            onchange: (event) => { d.weights[item.text] = Number(event.target.value); },
          }, [[1, "Matters a little"], [2, "Matters"], [3, "Matters a lot"]].map(([value, label]) =>
            h("option", { value: String(value), selected: (d.weights[item.text] || 2) === value ? true : null }, label)));
          return h("li", { class: "with-control" }, h("b", null, item.text), select);
        }))));
    }

    const remote = h("select", { "data-bind": "reading.remote_ok", onchange: (event) => { d.remote_ok = event.target.value; } },
      [["", "Not stated"], ["true", "Accepted"], ["false", "Ruled out"]].map(([value, label]) =>
        h("option", { value, selected: d.remote_ok === value ? true : null }, label)));
    parts.push(h("div", { class: "read-list" }, h("h4", null, "What search will look for"),
      h("div", { class: "grid" },
        field(d, "titles", "Job titles", "reading.titles", { rows: 2, wide: true, hint: "separated by commas" }),
        field(d, "keywords", "Keywords", "reading.keywords", { rows: 2, wide: true, hint: "separated by commas" }),
        field(d, "types", "Kinds of job", "reading.types", { hint: "separated by commas" }),
        field(d, "pay_floor", "Lowest pay", "reading.pay_floor"),
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Remote work"), remote),
        field(d, "locations", "Places", "reading.locations", { rows: 2, wide: true, hint: "one per line" }))));

    parts.push(h("div", { class: "step-actions" }, h("button", {
      type: "button", class: "btn btn-main", id: "btn-confirm-reading", disabled: busy || view.reading_stale,
      onclick: async () => {
        const patch = {
          titles: d.titles, keywords: d.keywords, types: d.types, locations: d.locations, pay_floor: d.pay_floor,
          remote_ok: d.remote_ok === "" ? null : d.remote_ok === "true", weights: d.weights,
        };
        const result = await send("PUT", "/api/requirements/reading", patch);
        if (result.ok) ctx.toast("Your requirements are confirmed.");
      },
    }, "Looks right")));
    return h("div", { class: "reading" }, parts);
  }

  function requirementsStep(view) {
    const { h } = ctx;
    const busy = view.busy.requirements;
    const parts = [
      h("p", { class: "step-lead" }, "Say it in your own words, one per line."),
      h("div", { class: "grid two" },
        field(draft, "hard", "Hard requirements", "req.hard", {
          rows: 6, hint: "a job must meet every one to be listed",
          placeholder: "Must be an internship or entry level",
        }),
        field(draft, "soft", "Soft preferences", "req.soft", {
          rows: 6, hint: "these only change the ranking",
          placeholder: "Prefer pay of at least $30 an hour\nPrefer the job to be related to robotics",
        })),
    ];
    if (busy) parts.push(statusLine("pen", "Reading your requirements", "This usually takes under a minute.", true));
    else if (view.errors.requirements) parts.push(statusLine("red", "Problem", view.errors.requirements));
    parts.push(h("div", { class: "step-actions" },
      h("button", {
        type: "button", class: "btn btn-main", id: "btn-save-req", disabled: busy,
        onclick: () => send("PUT", "/api/requirements", { hard: draft.hard, soft: draft.soft }),
      }, "Save and read"),
      !busy && view.errors.requirements && (view.requirements.hard.length + view.requirements.soft.length > 0) &&
        h("button", { type: "button", class: "btn", onclick: () => send("POST", "/api/requirements/read") }, "Try reading again")));
    // While a new reading is on its way the old one is not shown, so nothing
    // typed into it can be lost when the new one arrives.
    if (!busy && view.reading && draft.reading && (view.reading.hard.length || view.reading.soft.length)) {
      parts.push(readingPanel(view));
    }
    return step(3, "What you are looking for", ...parts);
  }

  // ---------- draw ----------

  function draw() {
    const data = ctx.getState().data;
    const root = document.querySelector("#profile");
    if (!data) { root.replaceChildren(); return; }
    const view = data.profile;
    syncDrafts(view);
    const key = JSON.stringify([view, data.agents, rev]);
    if (key === lastKey && root.childElementCount) return;
    lastKey = key;

    const active = document.activeElement;
    const bind = active && active.dataset ? active.dataset.bind : null;
    const caret = bind && typeof active.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;

    const { h } = ctx;
    const parts = [];
    if (data.agents.signed_in === false) {
      parts.push(h("div", { class: "signin", id: "signin-notice" },
        statusLine("mark", "The agents are not signed in to Claude",
          `Reading a resume or your requirements needs it. Run '${((data.app && data.app.commands) || {}).signin || "jobapp signin"}' in the Job App folder, then check again.`),
        h("div", { class: "step-actions" }, h("button", {
          type: "button", class: "btn", id: "btn-recheck",
          onclick: async () => {
            const result = await send("POST", "/api/agents/recheck");
            if (result.ok) ctx.toast(result.data.agents.signed_in ? "Signed in. The agents can run." : "Still not signed in.");
          },
        }, "Check again"))));
    }
    if (view.ready) {
      parts.push(h("div", { class: "signin" }, statusLine("done", "All set",
        "Your details and requirements are confirmed. You can change them here at any time.")));
    }
    parts.push(resumeStep(view, data.agents), aboutStep(view), requirementsStep(view));
    root.replaceChildren(...parts);

    if (bind) {
      const again = root.querySelector(`[data-bind="${CSS.escape(bind)}"]`);
      if (again) {
        again.focus({ preventScroll: true });
        if (caret && again.setSelectionRange) {
          try { again.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ }
        }
      }
    }
  }

  return {
    init(context) { ctx = context; },
    draw,
  };
})();
