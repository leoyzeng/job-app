/* The "Answers" screen: your answers to application questions, saved as you go.

   Each one was saved when you changed a field in an application and clicked
   Continue or Mark as applied. The agent is offered them next time the same
   question comes up. Here you can correct, restrict or delete them. */
window.JobAppAnswers = (() => {
  "use strict";

  let ctx = null;          // { h, api, refresh, toast, day, getState }
  let answers = null;      // the list from /api/answers, null until loaded
  let loading = false;
  let count = null;        // answers.count from the last state, to know when to reload
  let filter = "";
  const editing = new Map();   // answer id -> text being typed
  const confirming = new Set(); // answer ids whose Delete was clicked once

  function init(context) { ctx = context; }

  async function load() {
    if (loading) return;
    loading = true;
    const result = await ctx.api("GET", "/api/answers");
    loading = false;
    if (result.ok) {
      answers = result.data.answers;
      if (ctx.getState().view === "answers") draw();
    }
  }

  function stateChanged() {
    const data = ctx.getState().data;
    const now = data && data.answers ? data.answers.count : null;
    if (now !== count) {
      count = now;
      if (answers !== null || ctx.getState().view === "answers") load();
    }
  }

  function asText(value) {
    if (Array.isArray(value)) return value.join(", ");
    if (value === true) return "Yes (ticked)";
    if (value === false) return "No (not ticked)";
    return String(value ?? "");
  }

  function fromText(text, original) {
    if (Array.isArray(original)) return text.split(",").map((part) => part.trim()).filter(Boolean);
    if (typeof original === "boolean") return /^(y|yes|true|ticked)/i.test(text.trim());
    return text;
  }

  async function save(answer, patch, message) {
    const result = await ctx.api("PUT", `/api/answers/${encodeURIComponent(answer.id)}`, patch);
    if (!result.ok) {
      ctx.toast((result.data && result.data.message) || "That change could not be saved.");
      return false;
    }
    Object.assign(answer, result.data.answer);
    if (message) ctx.toast(message);
    draw();
    return true;
  }

  async function remove(answer) {
    if (!confirming.has(answer.id)) {
      confirming.add(answer.id);
      draw();
      return;
    }
    confirming.delete(answer.id);
    const result = await ctx.api("DELETE", `/api/answers/${encodeURIComponent(answer.id)}`);
    if (!result.ok) {
      ctx.toast((result.data && result.data.message) || "That answer could not be deleted.");
      return;
    }
    answers = answers.filter((a) => a.id !== answer.id);
    ctx.toast("Deleted. The agent will ask you again next time.");
    draw();
  }

  function card(answer) {
    const { h } = ctx;
    const long = typeof answer.answer === "string" && answer.answer.length > 80;
    const typed = editing.has(answer.id) ? editing.get(answer.id) : asText(answer.answer);
    const changed = typed !== asText(answer.answer);
    const input = h(long ? "textarea" : "input", {
      class: "answer-input", id: `answer-${answer.id}`, rows: long ? "5" : null, type: long ? null : "text",
      oninput: (event) => { editing.set(answer.id, event.target.value); draw(true); },
    });
    input.value = typed;
    const wordings = answer.also_asked_as || [];
    const meta = [
      answer.times_used ? `Used ${answer.times_used} time${answer.times_used === 1 ? "" : "s"}` : "Not reused yet",
      answer.first_seen && answer.first_seen.at ? `first asked ${ctx.day(answer.first_seen.at)}` : "",
      answer.source === "user_correction" ? "you corrected the agent here" : "",
    ].filter(Boolean).join(", ");
    return h("li", { class: "answer", "data-id": answer.id },
      h("label", { class: "answer-q", for: `answer-${answer.id}` }, answer.question),
      wordings.length > 0 && h("p", { class: "answer-also" }, "Also asked as: ", wordings.join("; ")),
      input,
      (answer.options_seen || []).length > 0 && h("p", { class: "answer-also" }, "Choices seen: ",
        answer.options_seen.slice(0, 12).join("; ")),
      h("div", { class: "answer-controls" },
        h("label", { class: "tick" }, h("input", {
          type: "checkbox", checked: answer.sensitivity === "sensitive",
          onchange: (event) => save(answer, { sensitivity: event.target.checked ? "sensitive" : "normal" }),
        }), "Sensitive: only reused for the same question"),
        h("label", { class: "tick" }, h("input", {
          type: "checkbox", checked: answer.scope === "job_specific",
          onchange: (event) => save(answer, { scope: event.target.checked ? "job_specific" : "global" }),
        }), "Only for the job it was written for"),
        h("span", { class: "answer-buttons" },
          changed && h("button", { type: "button", class: "btn btn-main",
            onclick: async () => {
              if (await save(answer, { answer: fromText(typed, answer.answer) }, "Saved. The agent uses this from now on.")) {
                editing.delete(answer.id);
                draw();
              }
            } }, "Save"),
          changed && h("button", { type: "button", class: "btn btn-plain",
            onclick: () => { editing.delete(answer.id); draw(); } }, "Undo"),
          h("button", { type: "button", class: "btn" + (confirming.has(answer.id) ? " btn-danger" : " btn-plain"),
            onclick: () => remove(answer) }, confirming.has(answer.id) ? "Click again to delete" : "Delete"))),
      h("p", { class: "answer-meta" }, meta),
      (answer.history || []).length > 0 && h("details", { class: "answer-history" },
        h("summary", null, "Earlier answers"),
        h("ul", null, answer.history.slice().reverse().map((item) =>
          h("li", null, h("b", null, ctx.day(item.at)), " ", asText(item.answer))))));
  }

  function draw(keepFocus) {
    const { h } = ctx;
    const root = document.getElementById("answers");
    if (!root || !ctx) return;
    if (answers === null) {
      load();
      root.replaceChildren(h("section", { class: "step" }, h("p", null, "Loading your answers.")));
      return;
    }
    const focused = keepFocus ? document.activeElement : null;
    const focusId = focused && focused.id;
    const caret = focused && focused.selectionStart;
    const wanted = filter.trim().toLowerCase();
    const shown = answers
      .filter((a) => !wanted || `${a.question} ${(a.also_asked_as || []).join(" ")} ${asText(a.answer)}`.toLowerCase().includes(wanted))
      .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const sensitive = shown.filter((a) => a.sensitivity === "sensitive");
    const general = shown.filter((a) => a.sensitivity !== "sensitive" && a.scope !== "job_specific");
    const specific = shown.filter((a) => a.sensitivity !== "sensitive" && a.scope === "job_specific");
    const group = (title, note, items) => items.length > 0 && h("div", { class: "group" },
      h("h3", { class: "group-title" }, title), note && h("p", { class: "step-note" }, note),
      h("ul", { class: "answers" }, items.map(card)));
    const search = h("input", {
      type: "search", id: "answers-filter", class: "answers-filter", placeholder: "Find a question or answer",
      "aria-label": "Find a question or answer",
      oninput: (event) => { filter = event.target.value; draw(true); },
    });
    search.value = filter;
    root.replaceChildren(h("section", { class: "step" },
      h("h2", { class: "step-title" }, "Your saved answers"),
      h("p", { class: "step-lead" }, "When you fill or change a field in an application and click Continue or Mark as applied, "
        + "your answer is saved here. The agent is offered it the next time the same question comes up. "
        + "Correct one here and the agent uses the correction from then on."),
      answers.length === 0
        ? h("div", { class: "empty" }, h("h2", null, "No saved answers yet"),
            h("p", null, "Prepare an application, answer what the agent left for you, and click Continue."))
        : h("div", null, search,
            shown.length === 0 && h("p", { class: "step-note" }, "Nothing matches."),
            group("Sensitive answers", "Work authorization, sponsorship, pay, demographics and the like. These are only "
              + "ever filled with exactly what you gave, for the same question.", sensitive),
            group("Answers the agent can reuse", "", general),
            group("Written for one job", "Kept as samples of your writing for cover letters and long answers. "
              + "They are not filled into other applications.", specific))));
    if (focusId) {
      const again = document.getElementById(focusId);
      if (again) {
        again.focus();
        if (caret != null && again.setSelectionRange) again.setSelectionRange(caret, caret);
      }
    }
  }

  return { init, draw: () => draw(false), stateChanged, reload: load };
})();
