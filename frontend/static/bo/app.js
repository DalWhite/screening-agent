// Backoffice shell: navigation, global candidate search, process filter and router.

import { api, recruiterName, setRecruiterName } from "./api.js";
import { avatar, button, clear, debounce, emptyState, h, icon, iconButton, loc, modal, spinner, toast } from "./kit.js";
import { loadMeta, onProfileChange, openNewCandidate, parseHash, profileName, profiles, setProfile, store } from "./shared.js";
import { renderAnalytics } from "./views/analytics.js";
import { renderCandidate } from "./views/candidate.js";
import { renderCandidates } from "./views/candidates.js";
import { renderPipeline } from "./views/pipeline.js";
import { renderProcessEditor } from "./views/process_editor.js";
import { renderProcesses } from "./views/processes.js";
import { openQuickView } from "./views/quickview.js";

const NAV = [
  { id: "pipeline", label: "Pipeline", icon: "board" },
  { id: "candidates", label: "Candidatos", icon: "users" },
  { id: "analytics", label: "Analítica", icon: "chart" },
  { id: "processes", label: "Procesos", icon: "sliders" },
];

const main = document.getElementById("view");
let slashBound = false;
let teardown = null;

function renderShell() {
  clear(document.getElementById("nav"), NAV.map((item) =>
    h("a", { href: `#/${item.id}`, class: "nav-link", dataset: { route: item.id } }, icon(item.icon, 17), item.label)));

  const select = document.getElementById("profile-filter");
  clear(select,
    h("option", { value: "" }, "Todos los procesos"),
    profiles().map((p) => h("option", { value: p.id, selected: p.id === store.profile }, `${loc(p.role)} · ${p.company}`)));
  if (store.profile && !profiles().some((p) => p.id === store.profile)) setProfile("");
  select.value = store.profile;
  select.onchange = () => setProfile(select.value);

  clear(document.getElementById("topbar-actions"),
    button("Nuevo candidato", { icon: "userPlus", variant: "primary", onClick: openNewCandidate }));
  renderSearch();
  renderRecruiter();
}

function renderSearch() {
  const box = document.getElementById("global-search");
  const input = h("input", { type: "search", placeholder: "Buscar candidatos…  ( / )", "aria-label": "Buscar candidatos",
                             testid: "global-search", autocomplete: "off" });
  const results = h("div", { class: "search-results", hidden: true, role: "listbox" });
  let items = [];
  let active = -1;
  const paint = () => {
    clear(results, items.length ? items.map((c, i) => h("a", {
      href: `#/candidates/${c.id}`, class: i === active ? "active" : "", role: "option",
      onclick: (e) => { e.preventDefault(); hide(); openQuickView(c.id); },
    }, avatar(c.name), h("div", { class: "grow" }, h("div", {}, c.name || "Sin nombre"),
      h("div", { class: "muted small" }, `${profileName(c.profile_id)}${c.city ? " · " + c.city : ""}`)))) :
      h("div", { class: "muted small", style: { padding: "10px" } }, "Sin resultados"));
  };
  const hide = () => { results.hidden = true; active = -1; };
  const search = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { hide(); return; }
    const data = await api.get(`/api/bo/candidates?q=${encodeURIComponent(q)}&page_size=6`).catch(() => ({ items: [] }));
    items = data.items;
    active = items.length ? 0 : -1;
    results.hidden = false;
    paint();
  }, 200);
  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (results.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); paint(); e.preventDefault(); }
    if (e.key === "ArrowUp") { active = Math.max(0, active - 1); paint(); e.preventDefault(); }
    if (e.key === "Enter" && items[active]) { hide(); openQuickView(items[active].id); input.blur(); }
    if (e.key === "Escape") hide();
  });
  input.addEventListener("blur", () => setTimeout(hide, 150));
  if (!slashBound) {
    slashBound = true;
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        e.preventDefault();
        document.querySelector('[data-testid="global-search"]')?.focus();
      }
    });
  }
  clear(box, h("div", { class: "input-icon" }, icon("search", 15), input), results);
}

function renderRecruiter() {
  const box = document.getElementById("recruiter");
  const name = recruiterName();
  clear(box, avatar(name || "?"),
    h("div", { class: "who grow" }, h("strong", { testid: "recruiter-name" }, name || "Sin nombre"), h("span", { class: "muted small" }, "Reclutador/a")),
    iconButton("pencil", "Cambiar nombre", askName));
}

function askName() {
  const input = h("input", { value: recruiterName(), placeholder: "Tu nombre", "aria-label": "Tu nombre" });
  const save = button("Guardar", { variant: "primary", onClick: () => { setRecruiterName(input.value); renderRecruiter(); close(); toast("Nombre guardado"); } });
  const { close } = modal("¿Quién eres?", h("div", { class: "field" }, h("span", {}, "Nombre"), input), [save],
    { description: "Aparece en notas, mensajes y en el historial de actividad." });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") save.click(); });
}

async function route() {
  if (teardown) { teardown(); teardown = null; }
  const { parts, params } = parseHash();
  const [section, id] = parts;
  for (const link of document.querySelectorAll(".nav-link")) {
    const on = link.dataset.route === section;
    link.classList.toggle("active", on);
    if (on) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  clear(main, spinner());
  try {
    if (section === "pipeline") teardown = await renderPipeline(main, params);
    else if (section === "candidates" && id) teardown = await renderCandidate(main, id, params);
    else if (section === "candidates") teardown = await renderCandidates(main, params);
    else if (section === "analytics") teardown = await renderAnalytics(main, params);
    else if (section === "processes" && id) teardown = await renderProcessEditor(main, decodeURIComponent(id), params);
    else if (section === "processes") teardown = await renderProcesses(main, params);
    else clear(main, emptyState("Página no encontrada"));
  } catch (err) {
    console.error(err);
    clear(main, emptyState(`No se pudo cargar: ${err.message}`, "alert"));
  }
  main.focus({ preventScroll: true });
}

async function init() {
  try {
    await loadMeta();
  } catch (err) {
    clear(main, emptyState(`No se pudo conectar con el servidor: ${err.message}`, "alert"));
    return;
  }
  renderShell();
  window.addEventListener("hashchange", route);
  onProfileChange((profileId) => {
    document.getElementById("profile-filter").value = profileId;
    const { parts, params } = parseHash();
    if (params.has("profile_id")) {
      if (profileId) params.set("profile_id", profileId); else params.delete("profile_id");
      const query = params.toString();
      history.replaceState(null, "", `#/${parts.join("/")}${query ? "?" + query : ""}`);
    }
    route();
  });
  window.addEventListener("bo:meta-changed", async () => { await loadMeta(true); renderShell(); });
  if (!location.hash) location.hash = "#/pipeline";
  else route();
  if (!recruiterName()) askName();
}

init();
