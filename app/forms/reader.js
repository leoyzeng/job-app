/* Job App form reader. Runs inside one frame of a page and describes its form.

   Returns every field (label, kind, required, options, current value, error,
   section), the buttons, the headings and any step indicator. Each field's
   element is tagged with data-jobapp-key so the filler can find it again;
   the key comes from the frame, kind, label and position, so it stays the
   same when the page redraws itself. Nothing on the page is changed except
   those tags. */
(args) => {
  "use strict";
  const MARK = "data-jobapp-key";
  const BMARK = "data-jobapp-button";
  const frameKey = args.frameKey;

  const clean = (s) => String(s || "").replace(/[ \s]+/g, " ").trim();
  const MARKS = /\s*(\*|✱|\(required\))\s*$/i;

  function fnv(text) {
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, "0");
  }

  // Every open shadow root, so fields inside web components are found too.
  const ROOTS = [document];
  (function collect(root) {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) { ROOTS.push(el.shadowRoot); collect(el.shadowRoot); }
    }
  })(document);
  const all = (sel) => ROOTS.flatMap((root) => [...root.querySelectorAll(sel)]);
  const byId = (el, id) => (el.getRootNode && el.getRootNode().getElementById
    ? el.getRootNode().getElementById(id) : document.getElementById(id));

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") return false;
    const rects = el.getClientRects();
    return rects.length > 0 && rects[0].width > 0 && rects[0].height > 0;
  }

  const CONTROLS = "input, select, textarea, option, button, [role=listbox], [role=option], script, style, svg, " +
                   "[contenteditable=true]";
  function textOf(node) {
    if (!node) return "";
    const copy = node.cloneNode(true);
    copy.querySelectorAll(CONTROLS).forEach((e) => e.remove());
    return clean(copy.textContent);
  }
  function rawText(node) {  // keeps the asterisk that marks a required field
    if (!node) return "";
    const copy = node.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, option, button, [role=listbox], script, style, svg").forEach((e) => e.remove());
    return clean(copy.textContent);
  }

  /* Text of the nearest child of a container that comes before the child holding the controls. */
  function textBefore(container, controls) {
    let found = "";
    for (const child of container.children) {
      if (controls.some((c) => child === c || child.contains(c))) break;
      const text = rawText(child);
      if (text) found = text;
    }
    return found;
  }

  function labelText(label, control) {
    if (label.contains(control)) {
      const before = textBefore(label, [control]);
      if (before) return before;
    }
    return rawText(label);
  }

  function ariaLabel(el) {
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const parts = ids.map((id) => byId(el, id)).filter((n) => n && n !== el).map(rawText).filter(Boolean);
    if (parts.length) return clean(parts.join(" "));
    return clean(el.getAttribute("aria-label"));
  }

  function nearbyText(el) {
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      const text = textBefore(node, [el]);
      if (text) return text;
    }
    return "";
  }

  function rawLabel(el) {
    const aria = ariaLabel(el);
    if (aria) return aria;
    if (el.labels && el.labels.length) {
      const text = labelText(el.labels[0], el);
      if (text) return text;
    }
    if (el.id) {
      const root = el.getRootNode();
      const label = root.querySelector && root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return labelText(label, el);
    }
    const wrap = el.closest("label");
    if (wrap) return labelText(wrap, el);
    return nearbyText(el) || clean(el.getAttribute("placeholder")) || clean(el.getAttribute("title"));
  }

  function groupRawLabel(controls) {
    const first = controls[0];
    const fieldset = first.closest("fieldset");
    if (fieldset && controls.every((c) => fieldset.contains(c))) {
      const legend = fieldset.querySelector("legend");
      if (legend) return rawText(legend);
    }
    const group = first.closest("[role=radiogroup], [role=group]");
    if (group && controls.every((c) => group.contains(c))) {
      const text = ariaLabel(group);
      if (text) return text;
    }
    let node = first.parentElement;
    while (node && !controls.every((c) => node.contains(c))) node = node.parentElement;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const text = textBefore(node, controls);
      if (text) return text;
    }
    return "";
  }

  function optionLabel(input) {
    const aria = ariaLabel(input);
    if (aria) return aria;
    if (input.labels && input.labels.length) {
      const text = textOf(input.labels[0]);
      if (text) return text;
    }
    return clean(input.value) || clean(input.textContent);
  }

  function split(raw) {
    const required = MARKS.test(raw) || /[*✱]/.test(raw.slice(-4));
    let label = raw.replace(/\(required\)/ig, "").replace(/[*✱]+/g, " ");
    label = clean(label.replace(/\(optional\)\s*$/i, ""));
    return { label, required };
  }

  // --- sections, errors --------------------------------------------------------------

  // A legend labels its own group of fields, so it is not a section heading.
  const headings = all("h1, h2, h3, h4, h5, [role=heading]").filter(visible);
  function sectionOf(el) {
    let found = "";
    for (const h of headings) {
      if (h.getRootNode() !== el.getRootNode() && h.getRootNode() !== document) continue;
      const pos = h.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_CONTAINED_BY) continue;   // a legend around the field is its label
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) found = clean(h.textContent);
    }
    return found.slice(0, 120);
  }

  function boxOf(el) {
    return el.closest(".field, li, fieldset, [data-automation-id^=formField], .application-question, [role=group]")
      || el.parentElement;
  }

  function errorOf(el) {
    const texts = [];
    for (const id of (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)) {
      const node = byId(el, id);
      if (node && (node.getAttribute("role") === "alert" || /error|invalid/i.test(node.className) ||
                   el.getAttribute("aria-invalid") === "true")) texts.push(clean(node.textContent));
    }
    const box = boxOf(el);
    if (!texts.length && box) {
      const node = box.querySelector("[role=alert], [class*=error]:not(input):not(select):not(textarea)");
      if (node && visible(node)) texts.push(clean(node.textContent));
    }
    return texts.filter(Boolean).join(" ").slice(0, 300);
  }

  // --- values -----------------------------------------------------------------------------

  const PLACEHOLDER_OPTION = /^(select|choose|please select|pick|--|—|\.\.\.)/i;

  function selectOptions(select) {
    return [...select.options]
      .filter((o, i) => !(i === 0 && (o.value === "" || PLACEHOLDER_OPTION.test(clean(o.text)))))
      .map((o) => clean(o.text));
  }
  function selectValue(select) {
    const chosen = [...select.selectedOptions].filter((o) => !(o.value === "" && PLACEHOLDER_OPTION.test(clean(o.text)) || o.value === "" && o.index === 0));
    return select.multiple ? chosen.map((o) => clean(o.text)) : (chosen[0] ? clean(chosen[0].text) : "");
  }

  function comboValue(el) {
    if (el.tagName === "BUTTON") {
      const text = clean(el.innerText);
      return /^(select( one)?|choose|--)\b/i.test(text) ? "" : text;
    }
    // The chosen value is shown in an element next to the input (react-select and similar).
    // Look a few levels up, but never so far that another combobox's value is found.
    let box = el;
    for (let i = 0; i < 5 && box.parentElement; i++) {
      const up = box.parentElement;
      if (up.querySelectorAll("[role=combobox], button[aria-haspopup=listbox]").length > 1) break;
      box = up;
      const single = box.querySelector("[class*=single-value], [class*=singleValue], [class*=selected-value]");
      if (single) return clean(single.textContent);
      const multi = box.querySelectorAll("[class*=multi-value__label], [class*=multiValueLabel]");
      if (multi.length) return [...multi].map((m) => clean(m.textContent));
      if (box.querySelector("[class*=placeholder]")) return "";
    }
    if (el.value) return clean(el.value);
    const hidden = box && box.querySelector("input[type=hidden][name]");
    return hidden ? clean(hidden.value) : "";
  }

  function comboOptions(el) {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    let list = id ? byId(el, id) : null;
    if (!list && el.tagName === "BUTTON") {
      const next = el.nextElementSibling;
      if (next && next.getAttribute("role") === "listbox") list = next;
    }
    if (!list) return null;
    return [...list.querySelectorAll("[role=option]")].map((o) => clean(o.textContent)).filter(Boolean);
  }

  // --- the fields -----------------------------------------------------------------------------

  const TEXT_KINDS = { text: "text", email: "email", tel: "tel", url: "url", number: "number", search: "text",
                       date: "date", month: "month", week: "text", time: "time", "datetime-local": "text",
                       password: "password" };
  const fields = [];
  const counts = {};
  const done = new Set();

  function add(kind, el, extra, controls) {
    const { label, required } = split(extra.rawLabel || "");
    const name = el.getAttribute ? (el.getAttribute("name") || "") : "";
    const basis = label || name || el.id || kind;
    const count = (counts[kind + "|" + basis] = (counts[kind + "|" + basis] || 0) + 1);
    const key = "f_" + fnv(`${frameKey}|${kind}|${basis.toLowerCase()}|${count}`);
    el.setAttribute(MARK, key);
    (controls || []).forEach((c, i) => c.setAttribute(MARK, `${key}#${i}`));
    const field = {
      key, kind, label, required: required || Boolean(extra.required),
      options: extra.options === undefined ? [] : (extra.options || []),
      options_known: extra.options !== null && extra.options !== undefined,
      value: extra.value, name, id: el.id || "",
      placeholder: clean(el.getAttribute && el.getAttribute("placeholder")),
      autocomplete: clean(el.getAttribute && el.getAttribute("autocomplete")),
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
      readonly: Boolean(el.readOnly),
      visible: visible(el) || (controls || []).some(visible),
      section: sectionOf(el),
      error: errorOf(el),
      frame: frameKey,
      automation_id: clean(el.getAttribute && el.getAttribute("data-automation-id")),
      multiple: Boolean(el.multiple),
      accept: clean(el.getAttribute && el.getAttribute("accept")),
    };
    fields.push(field);
  }

  const isRequired = (el) => el.required || el.getAttribute("aria-required") === "true";

  const elements = all("input, select, textarea, [role=combobox], [role=radio], [role=checkbox], [role=textbox][contenteditable=true], " +
                       "button[aria-haspopup=listbox], button[aria-pressed]");
  for (const el of elements) {
    if (done.has(el) || el.closest("[role=listbox], [aria-hidden=true]")) continue;
    const tag = el.tagName;
    const role = el.getAttribute("role");
    const type = (el.getAttribute("type") || (tag === "INPUT" ? "text" : "")).toLowerCase();

    if (tag === "BUTTON" && el.hasAttribute("aria-pressed")) {
      const group = [...el.parentElement.children].filter((b) => b.tagName === "BUTTON" && b.hasAttribute("aria-pressed"));
      group.forEach((b) => done.add(b));
      if (group.length < 2 || !group.some(visible)) continue;
      const holder = el.closest("[role=group]") || el.parentElement;
      const pressed = group.find((b) => b.getAttribute("aria-pressed") === "true");
      add("choice", holder, { rawLabel: groupRawLabel(group), options: group.map((b) => clean(b.textContent)),
                              value: pressed ? clean(pressed.textContent) : "",
                              required: holder.getAttribute("aria-required") === "true" }, group);
      continue;
    }
    if ((tag === "INPUT" && role === "combobox") || (role === "combobox" && tag !== "INPUT" && tag !== "SELECT") ||
        (tag === "BUTTON" && el.getAttribute("aria-haspopup") === "listbox")) {
      done.add(el);
      if (!visible(el) && !visible(el.parentElement)) continue;
      add("combobox", el, { rawLabel: rawLabel(el), options: comboOptions(el), value: comboValue(el), required: isRequired(el) });
      continue;
    }
    if (tag === "INPUT") {
      if (["hidden", "submit", "button", "reset", "image"].includes(type)) continue;
      if (type === "file") {
        done.add(el);
        add("file", el, { rawLabel: rawLabel(el), value: [...(el.files || [])].map((f) => f.name), required: isRequired(el) });
        continue;
      }
      if (type === "radio") {
        const root = el.form || el.getRootNode();
        const group = el.name
          ? [...root.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)]
          : [el];
        group.forEach((r) => done.add(r));
        if (!group.some(visible) && !group.some((r) => r.labels && [...r.labels].some(visible))) continue;
        const checked = group.find((r) => r.checked);
        add("radio", group[0], { rawLabel: groupRawLabel(group), options: group.map(optionLabel),
                                 value: checked ? optionLabel(checked) : "", required: group.some(isRequired) }, group);
        continue;
      }
      if (type === "checkbox") {
        const root = el.form || el.getRootNode();
        let group = el.name ? [...root.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`)] : [el];
        if (group.length < 2) {
          const fieldset = el.closest("fieldset");
          const inside = fieldset ? [...fieldset.querySelectorAll("input[type=checkbox]")] : [];
          if (inside.length >= 2 && fieldset.querySelector("legend")) group = inside;
        }
        group.forEach((c) => done.add(c));
        if (!group.some(visible) && !group.some((c) => c.labels && [...c.labels].some(visible))) continue;
        if (group.length >= 2) {
          add("checkbox_group", group[0], { rawLabel: groupRawLabel(group), options: group.map(optionLabel),
                                            value: group.filter((c) => c.checked).map(optionLabel),
                                            required: group.some(isRequired) }, group);
        } else {
          add("checkbox", el, { rawLabel: optionLabel(el) || rawLabel(el), value: el.checked, required: isRequired(el) });
        }
        continue;
      }
      done.add(el);
      if (!visible(el)) continue;
      const kind = TEXT_KINDS[type] || "text";
      // A password never leaves the page: only whether something was typed.
      add(kind, el, { rawLabel: rawLabel(el), value: kind === "password" ? (el.value ? "(entered)" : "") : el.value,
                      required: isRequired(el) });
      continue;
    }
    if (tag === "SELECT") {
      done.add(el);
      if (!visible(el)) continue;
      add(el.multiple ? "multiselect" : "select", el,
          { rawLabel: rawLabel(el), options: selectOptions(el), value: selectValue(el), required: isRequired(el) });
      continue;
    }
    if (tag === "TEXTAREA" || (role === "textbox" && el.isContentEditable)) {
      done.add(el);
      if (!visible(el)) continue;
      add("textarea", el, { rawLabel: rawLabel(el), value: tag === "TEXTAREA" ? el.value : el.innerText, required: isRequired(el) });
      continue;
    }
    if (role === "radio") {
      const holder = el.closest("[role=radiogroup]") || el.parentElement;
      const group = [...holder.querySelectorAll("[role=radio]")];
      group.forEach((r) => done.add(r));
      if (!group.some(visible)) continue;
      const checked = group.find((r) => r.getAttribute("aria-checked") === "true");
      add("radio", holder, { rawLabel: ariaLabel(holder) || groupRawLabel(group), options: group.map((r) => ariaLabel(r) || clean(r.textContent)),
                             value: checked ? (ariaLabel(checked) || clean(checked.textContent)) : "",
                             required: holder.getAttribute("aria-required") === "true" }, group);
      continue;
    }
    if (role === "checkbox") {
      done.add(el);
      if (!visible(el)) continue;
      add("checkbox", el, { rawLabel: rawLabel(el) || clean(el.textContent), value: el.getAttribute("aria-checked") === "true",
                            required: isRequired(el) });
    }
  }

  // --- buttons, steps, blockers -------------------------------------------------------------

  const buttons = [];
  const buttonCounts = {};
  const BUTTONISH = /apply|submit|next|continue|back|previous|save|review|send|finish|add/i;
  for (const el of all("button, input[type=submit], input[type=button], [role=button], a")) {
    if (el.hasAttribute(MARK) || el.hasAttribute("aria-pressed") || el.getAttribute("aria-haspopup") === "listbox") continue;
    if (el.closest("[role=listbox], [role=option]") || !visible(el)) continue;
    const text = clean(el.tagName === "INPUT" ? el.value : (el.innerText || el.getAttribute("aria-label") || el.value));
    if (!text) continue;
    if (el.tagName === "A" && el.getAttribute("role") !== "button" && !BUTTONISH.test(text)) continue;
    const n = (buttonCounts[text] = (buttonCounts[text] || 0) + 1);
    const key = "b_" + fnv(`${frameKey}|${text.toLowerCase()}|${n}`);
    el.setAttribute(BMARK, key);
    buttons.push({
      key, text: text.slice(0, 120), tag: el.tagName.toLowerCase(), type: (el.getAttribute("type") || "").toLowerCase(),
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
      automation_id: clean(el.getAttribute("data-automation-id")),
      href: el.tagName === "A" ? (el.href || "") : "", in_form: Boolean(el.closest("form")), frame: frameKey,
    });
  }

  let progress = "";
  const active = all("[data-automation-id=progressBarActiveStep]")[0];
  if (active && clean(active.textContent)) progress = clean(active.textContent);
  if (!progress) {
    const match = /step\s+\d+\s+(of|\/)\s+\d+/i.exec((document.body && document.body.innerText || "").slice(0, 5000));
    if (match) progress = match[0];
  }
  const captcha = Boolean(document.querySelector(
    "iframe[src*=recaptcha], iframe[src*=hcaptcha], iframe[src*=turnstile], .g-recaptcha, .h-captcha, .cf-turnstile"));

  return {
    url: location.href, title: document.title, frame: frameKey, fields, buttons,
    headings: headings.map((h) => clean(h.textContent)).filter(Boolean).slice(0, 40),
    progress, captcha,
  };
}
