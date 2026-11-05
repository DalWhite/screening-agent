// DOM helpers and the backoffice component kit (buttons, menus, drawer, modal,
// switch, segmented control, toasts). Candidate-provided text always goes
// through textContent (never innerHTML), so transcripts cannot inject markup.

import { icon } from "./icons.js";

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "style" && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) k.startsWith("--") ? el.style.setProperty(k, v) : (el.style[k] = v);
    } else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "testid") el.setAttribute("data-testid", value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// ---------- formatting ----------

const rtf = new Intl.RelativeTimeFormat("es", { numeric: "auto" });

export function relTime(iso) {
  if (!iso) return "—";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

export function relTimeShort(iso) {
  if (!iso) return "—";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "ahora";
  if (mins < 60) return `${mins} min`;
  if (mins < 1440) return `${Math.round(mins / 60)} h`;
  return `${Math.round(mins / 1440)} d`;
}

export function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
}

export function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");
}

export function loc(text, lang = "es") {
  if (!text) return "";
  if (typeof text === "string") return text;
  return text[lang] || text.es || text.en || Object.values(text)[0] || "";
}

// ---------- visual atoms ----------

const PALETTE = ["indigo", "amber", "teal", "violet", "pink", "blue", "orange", "green"];

export function profileTone(profileId) {
  let hash = 0;
  for (const c of profileId || "") hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}

export function profileColor(profileId) {
  return `var(--c-${profileTone(profileId)})`;
}

export function chip(text, cls = "", attrs = {}) {
  return h("span", { class: `chip ${cls}`.trim(), ...attrs }, text);
}

export function dot(tone) {
  return h("span", { class: `dot tone-${tone || "gray"}`, "aria-hidden": "true" });
}

export function scoreBadge(score, big = false) {
  if (score === null || score === undefined) return h("span", { class: `score none ${big ? "big" : ""}`, title: "Sin puntuación" }, "—");
  const level = score >= 70 ? "high" : score >= 40 ? "mid" : "low";
  return h("span", { class: `score ${level} ${big ? "big" : ""}`, title: `Puntuación ${score}/100`, testid: "score" }, String(Math.round(score)));
}

export function avatar(name, size = "") {
  return h("span", { class: `avatar ${size}`.trim(), "aria-hidden": "true" }, initials(name));
}

export function button(label, { icon: iconName, variant = "secondary", size = "", onClick, title, ...attrs } = {}) {
  const cls = ["btn", `btn-${variant}`, size ? `btn-${size}` : "", !label ? "btn-icon" : ""].filter(Boolean).join(" ");
  return h("button", { type: "button", class: cls, title: title || null, onclick: onClick, ...attrs },
    iconName ? icon(iconName, size === "sm" ? 14 : 16) : null, label ? h("span", {}, label) : null);
}

export function iconButton(iconName, label, onClick, attrs = {}) {
  return button("", { icon: iconName, variant: "ghost", size: "sm", onClick, "aria-label": label, title: label, ...attrs });
}

export function linkButton(label, href, { icon: iconName, variant = "secondary", size = "", ...attrs } = {}) {
  return h("a", { class: `btn btn-${variant} ${size ? `btn-${size}` : ""}`, href, ...attrs },
    iconName ? icon(iconName, size === "sm" ? 14 : 16) : null, h("span", {}, label));
}

export function switchInput({ checked = false, label, onChange, testid, hint }) {
  const input = h("input", { type: "checkbox", role: "switch", checked, testid });
  input.addEventListener("change", () => onChange?.(input.checked));
  return h("label", { class: "switch" }, input, h("span", { class: "switch-track", "aria-hidden": "true" }),
    h("span", { class: "switch-label" }, label, hint ? h("span", { class: "muted small" }, hint) : null));
}

export function segmented(options, value, onChange, label) {
  const group = h("div", { class: "segmented", role: "radiogroup", "aria-label": label });
  for (const [v, text] of options) {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(v === value), class: v === value ? "on" : "" }, text);
    b.addEventListener("click", () => {
      for (const other of group.children) { other.setAttribute("aria-checked", "false"); other.classList.remove("on"); }
      b.setAttribute("aria-checked", "true");
      b.classList.add("on");
      onChange(v);
    });
    group.appendChild(b);
  }
  return group;
}

// ---------- popovers & menus ----------

let openPopover = null;

export function closePopover() {
  if (openPopover) { openPopover.remove(); openPopover = null; }
}

export function popover(anchor, content, { align = "start", width } = {}) {
  closePopover();
  const box = h("div", { class: "popover", style: width ? { width: `${width}px` } : {} }, content);
  document.body.appendChild(box);
  const r = anchor.getBoundingClientRect();
  const bw = box.offsetWidth;
  let left = align === "end" ? r.right - bw : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
  let top = r.bottom + 6;
  if (top + box.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - box.offsetHeight - 6);
  box.style.left = `${left + window.scrollX}px`;
  box.style.top = `${top + window.scrollY}px`;
  openPopover = box;
  setTimeout(() => {
    const close = (e) => {
      if (box.contains(e.target) || anchor.contains(e.target)) return;
      document.removeEventListener("mousedown", close);
      if (openPopover === box) closePopover();
    };
    document.addEventListener("mousedown", close);
  }, 0);
  box.querySelector("input, button, [tabindex]")?.focus();
  return box;
}

export function menu(anchor, items, opts = {}) {
  const list = h("div", { class: "menu", role: "menu" }, items.filter(Boolean).map((it) => {
    if (it === "separator") return h("div", { class: "menu-sep", role: "separator" });
    if (it.header) return h("div", { class: "menu-header" }, it.header);
    return h("button", {
      type: "button", role: "menuitem", class: `menu-item ${it.danger ? "danger" : ""} ${it.checked ? "checked" : ""}`,
      disabled: it.disabled, testid: it.testid,
      onclick: () => { closePopover(); it.onClick?.(); },
    }, it.tone ? dot(it.tone) : it.icon ? icon(it.icon, 15) : h("span", { class: "menu-icon-space" }),
    h("span", { class: "menu-label" }, it.label), it.checked ? icon("check", 15, "menu-check") : null,
    it.hint ? h("span", { class: "menu-hint" }, it.hint) : null);
  }));
  return popover(anchor, list, opts);
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });

// ---------- feedback ----------

export function toast(message, kind = "ok") {
  let root = document.getElementById("toasts");
  if (!root) {
    root = h("div", { id: "toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(root);
  }
  const el = h("div", { class: `toast ${kind}` }, icon(kind === "error" ? "alert" : "check", 16), h("span", {}, message));
  root.appendChild(el);
  setTimeout(() => el.classList.add("out"), 2800);
  setTimeout(() => el.remove(), 3300);
}

export function modal(title, body, actions = [], { description } = {}) {
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const dialog = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", {}, h("div", {}, h("h2", {}, title), description ? h("p", { class: "muted" }, description) : null),
      iconButton("x", "Cerrar", close)),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("footer", {}, button("Cancelar", { variant: "ghost", onClick: close }), actions) : null,
  );
  const backdrop = h("div", { class: "backdrop", onmousedown: (e) => { if (e.target === backdrop) close(); } }, dialog);
  document.addEventListener("keydown", esc);
  document.body.appendChild(backdrop);
  const first = dialog.querySelector(".modal-body input, .modal-body select, .modal-body textarea, footer .btn-primary");
  if (first) first.focus();
  return { close, dialog };
}

export function confirmDialog(title, message, { confirmLabel = "Confirmar", danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const ok = button(confirmLabel, { variant: danger ? "danger" : "primary", onClick: () => { done = true; close(); resolve(true); } });
    const { close, dialog } = modal(title, h("p", {}, message), [ok]);
    const observer = new MutationObserver(() => {
      if (!document.body.contains(dialog)) { observer.disconnect(); if (!done) resolve(false); }
    });
    observer.observe(document.body, { childList: true });
  });
}

export function drawer(title, body, { width = 520, onClose, actions = [], subtitle } = {}) {
  document.querySelector(".drawer-backdrop")?.remove();
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", esc); onClose?.(); };
  const esc = (e) => { if (e.key === "Escape" && !openPopover) close(); };
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title, style: { width: `${width}px` } },
    h("header", { class: "drawer-head" },
      h("div", { class: "drawer-title" }, h("h2", {}, title), subtitle ? h("div", { class: "muted small" }, subtitle) : null),
      h("div", { class: "drawer-actions" }, actions, iconButton("x", "Cerrar panel", close))),
    h("div", { class: "drawer-body" }, body));
  const backdrop = h("div", { class: "drawer-backdrop", onmousedown: (e) => { if (e.target === backdrop) close(); } }, panel);
  document.addEventListener("keydown", esc);
  document.body.appendChild(backdrop);
  requestAnimationFrame(() => panel.classList.add("open"));
  return { close, panel, body: panel.querySelector(".drawer-body") };
}

export function emptyState(text, iconName = "search", action = null) {
  return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon(iconName, 22)), h("p", {}, text), action);
}

export function spinner() {
  return h("div", { class: "loading", role: "progressbar", "aria-label": "Cargando" }, h("span"), h("span"), h("span"));
}

export function sectionTitle(title, sub, ...actions) {
  return h("div", { class: "section-title" }, h("div", {}, h("h3", {}, title), sub ? h("p", { class: "muted small" }, sub) : null),
    actions.length ? h("div", { class: "row" }, actions) : null);
}
