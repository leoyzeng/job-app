/* Shared parts of the practice application forms.

   These pages copy how real applicant systems build their forms, because
   that is what the form engine has to cope with:
   - "controlled" inputs: the page keeps its own copy of every value and
     redraws fields from it, so a value set without the proper events is lost;
   - comboboxes in the style of react-select (Greenhouse), with a menu that
     only exists while open and a separate element showing the choice;
   - yes/no toggle buttons (Ashby), dropdown buttons with listboxes (Workday).
   Nothing on these pages is ever sent anywhere. */
(() => {
  "use strict";

  const state = {};
  const views = [];       // functions that redraw a field from state
  const listeners = [];

  function set(key, value) {
    state[key] = value;
    redraw();
    for (const fn of listeners) fn(key, value);
  }
  function redraw() { for (const fn of views) fn(); }

  /* A text input or textarea whose value lives in state, like a React controlled input. */
  function controlled(el, key) {
    state[key] = el.value || "";
    el.addEventListener("input", () => set(key, el.value));
    views.push(() => { if (el.value !== state[key]) el.value = state[key]; });
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  let comboCount = 0;

  /* A react-select style combobox (Greenhouse's newer forms).
     options: an array, or a function(text) returning a Promise of an array (search as you type). */
  function combobox({ id, name, label, options, required, placeholder = "Select...", minChars = 0 }) {
    comboCount += 1;
    const n = comboCount;
    const listId = `react-select-${n}-listbox`;
    const key = name || id;
    state[key] = "";
    const hidden = el("input", { type: "hidden", name: name || id, value: "" });
    const input = el("input", {
      id, class: "select__input", type: "text", role: "combobox", autocomplete: "off",
      "aria-autocomplete": "list", "aria-expanded": "false", "aria-haspopup": "true",
      "aria-labelledby": `${id}-label`, "aria-required": required ? "true" : null,
    });
    const shown = el("div", { class: "select__placeholder", text: placeholder });
    const valueBox = el("div", { class: "select__value-container" }, shown, input);
    const control = el("div", { class: "select__control" }, valueBox, el("div", { class: "select__indicators", text: "▾" }));
    const box = el("div", { class: "select__container" }, control, hidden);
    let menu = null;
    let active = 0;
    let current = [];
    let token = 0;

    function close() {
      if (menu) { menu.remove(); menu = null; }
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-controls");
      input.removeAttribute("aria-activedescendant");
    }
    function choose(text) {
      set(key, text);
      input.value = "";
      close();
    }
    function drawMenu(list) {
      current = list;
      if (!menu) {
        menu = el("div", { class: "select__menu", id: listId, role: "listbox" });
        box.append(menu);
      }
      menu.replaceChildren(...(list.length ? list.map((text, i) => el("div", {
        class: "select__option" + (i === active ? " select__option--is-focused" : ""),
        id: `react-select-${n}-option-${i}`, role: "option", "aria-selected": text === state[key] ? "true" : "false",
        onmousedown: (event) => { event.preventDefault(); choose(text); },
      }, text)) : [el("div", { class: "select__menu-notice", text: "No options" })]));
      input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-controls", listId);
    }
    async function open() {
      const typed = input.value.trim().toLowerCase();
      const mine = ++token;
      let list;
      if (typeof options === "function") {
        if (typed.length < minChars) { drawMenu([]); return; }
        list = await options(typed);
        if (mine !== token) return;
      } else {
        list = options.filter((o) => o.toLowerCase().includes(typed));
      }
      active = 0;
      drawMenu(list);
    }
    control.addEventListener("mousedown", (event) => {
      if (event.target !== input) event.preventDefault();
      input.focus();
      menu ? close() : open();
    });
    input.addEventListener("input", () => { shown.style.display = input.value ? "none" : ""; open(); });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); if (!menu) open(); else { active = Math.min(active + 1, current.length - 1); drawMenu(current); } }
      else if (event.key === "Enter") { event.preventDefault(); if (menu && current[active]) choose(current[active]); }
      else if (event.key === "Escape") close();
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
    views.push(() => {
      hidden.value = state[key];
      shown.className = state[key] ? "select__single-value" : "select__placeholder";
      shown.textContent = state[key] || placeholder;
      shown.style.display = input.value ? "none" : "";
    });
    return el("div", { class: "field" },
      el("label", { id: `${id}-label`, for: id }, label, required ? el("span", { class: "asterisk", "aria-hidden": "true", text: " *" }) : null),
      box);
  }

  /* Yes/No style buttons (Ashby). */
  function toggles({ id, label, options, required }) {
    state[id] = "";
    const buttons = options.map((text) => el("button", {
      type: "button", class: "toggle", "aria-pressed": "false",
      onclick: () => set(id, state[id] === text ? "" : text),
    }, text));
    views.push(() => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === state[id]))));
    return el("div", { class: "field", role: "group", "aria-labelledby": `${id}-label` },
      el("div", { class: "question", id: `${id}-label` }, label, required ? el("span", { class: "asterisk", text: " *" }) : null),
      el("div", { class: "toggles" }, buttons));
  }

  /* A dropdown button with a listbox (Workday). */
  function dropdown({ id, label, options, required }) {
    if (!(id in state)) state[id] = "";   // Workday redraws a step's fields; the value stays
    const button = el("button", {
      type: "button", id, "aria-haspopup": "listbox", "aria-expanded": "false",
      "data-automation-id": id, "aria-labelledby": `${id}-label ${id}`, class: "wd-dropdown",
      "aria-required": required ? "true" : null,
    }, "Select One");
    let list = null;
    function close() { if (list) { list.remove(); list = null; } button.setAttribute("aria-expanded", "false"); }
    button.addEventListener("click", () => {
      if (list) { close(); return; }
      list = el("ul", { role: "listbox", class: "wd-list", id: `${id}-list` }, options.map((text) => el("li", {
        role: "option", "aria-selected": String(text === state[id]), tabindex: "-1",
        onclick: () => { set(id, text); close(); },
      }, text)));
      button.after(list);
      button.setAttribute("aria-expanded", "true");
    });
    views.push(() => { button.textContent = state[id] || "Select One"; });
    return el("div", { class: "field", "data-automation-id": `formField-${id}` },
      el("label", { id: `${id}-label`, for: id }, label, required ? el("abbr", { title: "required", text: "*" }) : null),
      button);
  }

  /* The submit button on every practice form does nothing but say so. */
  function practiceSubmit(button) {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      let note = document.querySelector("#practice-sent");
      if (!note) {
        note = el("p", { id: "practice-sent", class: "sent", role: "status" });
        button.after(note);
      }
      note.textContent = "Thank you for applying! (This is a practice form: nothing was sent anywhere.)";
    });
  }

  const PLACES = ["Pittsburgh, Pennsylvania, United States", "Pittsburg, Kansas, United States",
    "Philadelphia, Pennsylvania, United States", "Boston, Massachusetts, United States",
    "San Francisco, California, United States", "Seattle, Washington, United States",
    "Toronto, Ontario, Canada", "Waterloo, Ontario, Canada"];

  function places(text) {
    return new Promise((resolve) => setTimeout(() => resolve(PLACES.filter((p) => p.toLowerCase().includes(text))), 250));
  }

  window.Practice = { state, set, redraw, controlled, el, combobox, toggles, dropdown, practiceSubmit, places,
                      onChange: (fn) => listeners.push(fn) };
})();
