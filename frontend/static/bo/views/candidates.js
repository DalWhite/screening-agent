// Candidate list. One compact filter row (search + the three filters used daily);
// everything else lives behind "Más filtros". Active filters show as removable
// pills. Every filter is in the URL, so views can be bookmarked and analytics
// can link straight into a filtered list.

import { api, qs } from "../api.js";
import {
  avatar, button, chip, clear, debounce, emptyState, h, icon, iconButton, linkButton, loc, popover, relTimeShort,
  scoreBadge, toast,
} from "../kit.js";
import {
  candidateHref, faqLabel, flagChips, flagLabel, loadMeta, pipelineLabel, pipelineStages, profileById, profileChip, profiles,
  setProfile, sourceLabel, stagePill, statusChip, statusLabel, store,
} from "../shared.js";
import { openQuickView } from "./quickview.js";

const LIST_KEYS = ["pipeline_stage", "status", "source", "language", "flag", "tag", "disqualify_reason", "dropped_at"];
const SCALAR_KEYS = ["q", "assignee", "qualified", "bot_paused", "min_score", "max_score", "started_from",
                     "started_to", "city", "reached", "faq", "sort", "order", "page"];
const ADVANCED = ["source", "language", "tag", "assignee", "qualified", "bot_paused", "min_score", "max_score",
                  "started_from", "started_to", "city"];

const VIEWS = [
  { id: "all", label: "Todos", params: {} },
  { id: "review", label: "Por revisar", params: { pipeline_stage: ["review"] } },
  { id: "qualified", label: "Pre-calificados", params: { pipeline_stage: ["qualified"], sort: "score", order: "desc" } },
  { id: "waiting", label: "Esperando respuesta", params: { bot_paused: "true" } },
  { id: "alerts", label: "Con alertas", params: { flag: ["needs_review", "requested_human_callback", "physical_review", "self_harm", "check_error"] } },
  { id: "top", label: "Mejor puntuados", params: { min_score: "70", sort: "score", order: "desc" } },
];

const SORTS = [["last_ts:desc", "Última actividad"], ["started_at:desc", "Más recientes"], ["started_at:asc", "Más antiguos"],
               ["score:desc", "Mayor puntuación"], ["score:asc", "Menor puntuación"], ["name:asc", "Nombre A-Z"], ["city:asc", "Ciudad A-Z"]];

function readFilters(params) {
  const f = {};
  for (const key of LIST_KEYS) f[key] = (params.get(key) || "").split(",").filter(Boolean);
  for (const key of SCALAR_KEYS) f[key] = params.get(key) || "";
  f.sort = f.sort || "last_ts";
  f.order = f.order || "desc";
  f.page = Number(f.page || 1);
  return f;
}

function questionLabelAnywhere(id) {
  for (const p of profiles()) {
    const q = p.questions.find((x) => x.id === id);
    if (q) return loc(q.label);
  }
  return { intro: "Inicio", review: "Final", qualified: "Pre-calificado" }[id] || id;
}

export async function renderCandidates(root, params) {
  const meta = await loadMeta();
  if (params.has("profile_id") && params.get("profile_id") !== store.profile) {
    setProfile(params.get("profile_id"));
    return () => {};
  }
  const f = readFilters(params);
  const selected = new Set();
  let facets = {};
  let moreOpen = localStorage.getItem("bo_more_filters") === "1";

  const count = h("p", { class: "muted", testid: "result-count" });
  const viewsEl = h("div", { class: "views", role: "tablist", "aria-label": "Vistas rápidas" });
  const filterBar = h("div", { class: "filter-bar" });
  const morePanel = h("div", { class: "more-filters", testid: "more-filters" });
  const activeEl = h("div", { class: "active-filters", testid: "active-filters" });
  const results = h("div", { class: "results", testid: "results" });
  const bulkBar = h("div", { class: "bulk-bar", hidden: true, testid: "bulk-bar" });
  const exportBtn = h("a", { class: "btn btn-secondary", testid: "export-csv", download: "candidatos.csv" }, icon("download", 16), h("span", {}, "Exportar CSV"));

  const setUrl = () => {
    const query = qs({ ...f, page: f.page > 1 ? f.page : "", profile_id: store.profile,
                       sort: f.sort === "last_ts" ? "" : f.sort, order: f.order === "desc" ? "" : f.order });
    history.replaceState(null, "", `#/candidates${query ? "?" + query : ""}`);
  };
  const apply = () => { f.page = 1; setUrl(); renderChrome(); load(); };
  const applySoon = debounce(apply, 300);
  const reset = () => {
    for (const k of LIST_KEYS) f[k] = [];
    for (const k of SCALAR_KEYS) f[k] = "";
    Object.assign(f, { sort: "last_ts", order: "desc", page: 1 });
  };

  // ---------- filter widgets ----------

  function multiChip(label, key, options, facetKey) {
    const values = f[key];
    const text = values.length === 0 ? label
      : values.length === 1 ? `${label}: ${options.find(([v]) => v === values[0])?.[1] || values[0]}`
        : `${label}: ${values.length}`;
    const btn = h("button", { type: "button", class: `filter-chip ${values.length ? "on" : ""}`, "aria-haspopup": "dialog",
                              testid: `filter-${key}` }, icon(values.length ? "check" : "plus", 14), h("span", {}, text));
    btn.addEventListener("click", () => {
      const list = h("div", { class: "check-list", role: "group", "aria-label": label }, options.map(([value, text]) => {
        const box = h("input", { type: "checkbox", checked: f[key].includes(value) });
        box.addEventListener("change", () => {
          f[key] = box.checked ? [...f[key], value] : f[key].filter((v) => v !== value);
          f.page = 1; setUrl(); load(); renderChrome(false);
        });
        const n = facetKey ? (facets[facetKey]?.[value] ?? 0) : undefined;
        return h("label", {}, box, h("span", {}, text), n !== undefined ? h("span", { class: "facet", "aria-hidden": "true" }, String(n)) : null);
      }));
      popover(btn, h("div", {}, list, h("div", { class: "popover-foot" },
        button("Limpiar", { variant: "ghost", size: "sm", onClick: () => { f[key] = []; apply(); } }),
        button("Listo", { variant: "secondary", size: "sm", onClick: () => { document.querySelector(".popover")?.remove(); renderChrome(); } }))), { width: 260 });
    });
    return btn;
  }

  function field(label, input) {
    return h("label", { class: "field" }, h("span", {}, label), input);
  }

  function textInput(key, attrs = {}) {
    const el = h("input", { value: f[key], ...attrs });
    el.addEventListener("input", () => { f[key] = el.value.trim(); applySoon(); });
    return el;
  }

  function selectInput(key, options, label, single = false) {
    const value = single ? (f[key][0] || "") : f[key];
    const el = h("select", { "aria-label": label }, options.map(([v, l]) => h("option", { value: v, selected: value === v }, l)));
    el.addEventListener("change", () => { f[key] = single ? (el.value ? [el.value] : []) : el.value; apply(); });
    return el;
  }

  function renderMore() {
    clear(morePanel,
      field("Canal", selectInput("source", [["", "Todos"], ...Object.keys(meta.sources).map((s) => [s, sourceLabel(s)])], "Canal", true)),
      field("Idioma", selectInput("language", [["", "Todos"], ["es", "Español"], ["en", "English"]], "Idioma", true)),
      field("Puntuación", h("div", { class: "range" },
        textInput("min_score", { type: "number", min: 0, max: 100, placeholder: "mín", "aria-label": "Puntuación mínima" }),
        textInput("max_score", { type: "number", min: 0, max: 100, placeholder: "máx", "aria-label": "Puntuación máxima" }))),
      field("Inicio del screening", h("div", { class: "range" },
        textInput("started_from", { type: "date", "aria-label": "Desde" }), textInput("started_to", { type: "date", "aria-label": "Hasta" }))),
      field("Ciudad", textInput("city", { placeholder: "Ej. Madrid", "aria-label": "Ciudad" })),
      field("Etiqueta", selectInput("tag", [["", "Cualquiera"], ...(meta.tags || []).map((t) => [t, t])], "Etiqueta", true)),
      field("Responsable", selectInput("assignee", [["", "Cualquiera"], ["__none__", "Sin asignar"], ...(meta.assignees || []).map((a) => [a, a])], "Responsable")),
      field("Calificado por el asistente", selectInput("qualified", [["", "Todos"], ["true", "Sí"], ["false", "No"]], "Calificado por el bot")),
      field("Conversación", selectInput("bot_paused", [["", "Todas"], ["true", "Atendida por reclutador"], ["false", "Atendida por el asistente"]], "Conversación")));
  }

  function activePills() {
    const pills = [];
    const add = (key, text, value) => pills.push(chip([h("span", {}, text), h("button", {
      type: "button", "aria-label": `Quitar filtro ${text}`,
      onclick: () => { if (value !== undefined) f[key] = f[key].filter((v) => v !== value); else f[key] = Array.isArray(f[key]) ? [] : ""; apply(); },
    }, icon("x", 12))]));
    const labelers = {
      pipeline_stage: ["Etapa", pipelineLabel], status: ["Resultado", statusLabel], flag: ["Alerta", flagLabel],
      source: ["Canal", sourceLabel], language: ["Idioma", (v) => ({ es: "Español", en: "English" }[v] || v)], tag: ["Etiqueta", (v) => v],
      disqualify_reason: ["Motivo de descarte", flagLabel], dropped_at: ["Abandonó en", questionLabelAnywhere],
    };
    for (const [key, [label, fn]] of Object.entries(labelers)) for (const v of f[key]) add(key, `${label}: ${fn(v)}`, v);
    if (f.q) add("q", `Texto: «${f.q}»`);
    if (f.reached) add("reached", `Llegó a: ${questionLabelAnywhere(f.reached)}`);
    if (f.faq) add("faq", `Preguntó: ${faqLabel(f.faq)}`);
    if (f.min_score) add("min_score", `Puntuación ≥ ${f.min_score}`);
    if (f.max_score) add("max_score", `Puntuación ≤ ${f.max_score}`);
    if (f.started_from) add("started_from", `Desde ${f.started_from}`);
    if (f.started_to) add("started_to", `Hasta ${f.started_to}`);
    if (f.city) add("city", `Ciudad: ${f.city}`);
    if (f.assignee) add("assignee", `Responsable: ${f.assignee === "__none__" ? "sin asignar" : f.assignee}`);
    if (f.qualified) add("qualified", `Calificado por el asistente: ${f.qualified === "true" ? "sí" : "no"}`);
    if (f.bot_paused) add("bot_paused", f.bot_paused === "true" ? "Atendida por reclutador" : "Atendida por el asistente");
    return pills;
  }

  function activeView() {
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    return VIEWS.find((v) => {
      const want = { ...readFilters(new URLSearchParams()), ...v.params };
      return LIST_KEYS.every((k) => same([...(want[k] || [])].sort(), [...f[k]].sort()))
        && SCALAR_KEYS.filter((k) => k !== "page").every((k) => String(want[k] || "") === String(f[k] || ""));
    })?.id;
  }

  function renderChrome(full = true) {
    const current = activeView();
    clear(viewsEl, VIEWS.map((v) => h("button", {
      type: "button", role: "tab", "aria-selected": String(v.id === current), class: v.id === current ? "on" : "",
      onclick: () => { reset(); Object.assign(f, v.params); apply(); },
    }, v.label)));
    const search = h("input", { type: "search", value: f.q, placeholder: "Buscar nombre, ciudad, notas, conversación…",
                                "aria-label": "Buscar candidatos", testid: "search" });
    search.addEventListener("input", () => { f.q = search.value.trim(); f.page = 1; setUrl(); debouncedLoad(); });
    const advancedCount = ADVANCED.filter((k) => (Array.isArray(f[k]) ? f[k].length : f[k])).length;
    const moreBtn = h("button", { type: "button", class: `filter-chip ${moreOpen ? "on" : ""}`, "aria-expanded": String(moreOpen),
                                  testid: "more-filters-toggle" },
      icon("filter", 14), h("span", {}, "Más filtros"), advancedCount ? h("span", { class: "count" }, String(advancedCount)) : null);
    moreBtn.addEventListener("click", () => {
      moreOpen = !moreOpen;
      localStorage.setItem("bo_more_filters", moreOpen ? "1" : "0");
      renderChrome();
    });
    const sort = h("select", { "aria-label": "Ordenar por" }, SORTS.map(([v, l]) => h("option", { value: v, selected: v === `${f.sort}:${f.order}` }, l)));
    sort.addEventListener("change", () => { [f.sort, f.order] = sort.value.split(":"); apply(); });
    if (full || !filterBar.contains(document.activeElement)) {
      clear(filterBar,
        h("div", { class: "input-icon" }, icon("search", 15), search),
        multiChip("Etapa", "pipeline_stage", pipelineStages().map((s) => [s.id, loc(s.label)]), "pipeline_stage"),
        multiChip("Resultado", "status", Object.keys(meta.statuses).map((s) => [s, statusLabel(s)]), "status"),
        multiChip("Alertas", "flag", Object.entries(meta.flags || {}).sort((a, b) => a[1].localeCompare(b[1]))),
        moreBtn,
        h("div", { class: "grow" }),
        sort);
    }
    morePanel.hidden = !moreOpen;
    if (moreOpen && full) renderMore();
    const pills = activePills();
    clear(activeEl, pills, pills.length ? button("Limpiar filtros", { icon: "x", variant: "ghost", size: "sm", testid: "clear-filters",
      onClick: () => { reset(); apply(); } }) : null);
    activeEl.hidden = !pills.length;
  }

  // ---------- results ----------

  function renderBulk() {
    bulkBar.hidden = selected.size === 0;
    if (!selected.size) return;
    const ids = [...selected];
    const move = h("select", { "aria-label": "Mover seleccionados a" }, h("option", { value: "" }, "Mover a…"),
      pipelineStages().map((s) => h("option", { value: s.id }, loc(s.label))));
    move.addEventListener("change", async () => {
      if (!move.value) return;
      await api.post("/api/bo/candidates/bulk", { ids, action: "move", value: move.value });
      toast(`${ids.length} candidato(s) movidos a «${pipelineLabel(move.value)}»`);
      selected.clear();
      load();
    });
    const tagInput = h("input", { placeholder: "Etiqueta…", "aria-label": "Etiquetar seleccionados", list: "bulk-tags" });
    const tagBtn = button("Etiquetar", { variant: "ghost", size: "sm", onClick: async () => {
      const tag = tagInput.value.trim();
      if (!tag) return;
      await api.post("/api/bo/candidates/bulk", { ids, action: "tag", value: tag });
      await loadMeta(true);
      toast(`Etiqueta «${tag}» añadida`);
      selected.clear();
      load();
    } });
    clear(bulkBar, h("strong", {}, `${ids.length} seleccionado(s)`), move,
      h("datalist", { id: "bulk-tags" }, (meta.tags || []).map((t) => h("option", { value: t }))), tagInput, tagBtn,
      button("Quitar selección", { variant: "ghost", size: "sm", onClick: () => { selected.clear(); load(); } }));
  }

  function row(c) {
    const box = h("input", { type: "checkbox", checked: selected.has(c.id), "aria-label": `Seleccionar ${c.name || c.id}` });
    box.addEventListener("change", () => { box.checked ? selected.add(c.id) : selected.delete(c.id); renderBulk(); });
    const tr = h("tr", { class: "clickable", testid: "candidate-row", dataset: { id: c.id } },
      h("td", { onclick: (e) => e.stopPropagation() }, box),
      h("td", {}, h("div", { class: "name-cell" }, avatar(c.name),
        h("div", { class: "who" },
          h("a", { href: candidateHref(c.id), onclick: (e) => e.stopPropagation() }, c.name || "Sin nombre"),
          [c.city, c.contact].some(Boolean) ? h("span", { class: "muted small" }, [c.city, c.contact].filter(Boolean).join(" · ")) : null))),
      store.profile ? null : h("td", {}, profileChip(c.profile_id, true)),
      h("td", { class: "num" }, scoreBadge(c.score)),
      h("td", { onclick: (e) => e.stopPropagation() }, stagePill(c, () => load())),
      h("td", {}, statusChip(c.status)),
      h("td", { class: "flags-cell" }, c.awaiting_reply ? h("span", { class: "badge-reply" }, icon("message", 12), "Esperando respuesta") : null,
        flagChips(c.flags, 1), (c.tags || []).map((t) => chip(`#${t}`, "chip-tag"))),
      h("td", {}, c.assignee ? h("span", { class: "row", title: c.assignee }, avatar(c.assignee)) : h("span", { class: "muted" }, "—")),
      h("td", { class: "muted small", title: c.last_ts }, relTimeShort(c.last_ts)));
    tr.addEventListener("click", () => openQuickView(c.id));
    return tr;
  }

  let seq = 0;
  async function load() {
    const mine = ++seq;
    const query = qs({ ...f, profile_id: store.profile, page_size: 25 });
    exportBtn.href = `/api/bo/candidates.csv?${query}`;
    let data;
    try {
      data = await api.get(`/api/bo/candidates?${query}`);
    } catch (err) {
      if (mine === seq) clear(results, emptyState(err.message, "alert"));
      return;
    }
    if (mine !== seq) return;
    facets = data.facets || {};
    count.textContent = `${data.total} candidato${data.total === 1 ? "" : "s"}`;
    renderBulk();
    if (!data.items.length) {
      clear(results, h("div", { class: "table-wrap" }, emptyState("Ningún candidato coincide con estos filtros.", "search",
        activePills().length ? button("Limpiar filtros", { onClick: () => { reset(); apply(); } }) : null)));
      return;
    }
    const allBox = h("input", { type: "checkbox", "aria-label": "Seleccionar todos", checked: data.items.every((c) => selected.has(c.id)) });
    allBox.addEventListener("change", () => {
      for (const c of data.items) allBox.checked ? selected.add(c.id) : selected.delete(c.id);
      load();
    });
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    const from = (data.page - 1) * data.page_size + 1;
    const to = Math.min(data.total, data.page * data.page_size);
    clear(results,
      h("div", { class: "table-wrap" },
        h("table", { class: "table" },
          h("thead", {}, h("tr", {}, h("th", {}, allBox),
            ["Candidato", store.profile ? null : "Proceso", "Punt.", "Etapa", "Asistente", "Alertas", "Responsable", "Actividad"]
              .filter(Boolean).map((t) => h("th", { class: t === "Punt." ? "num" : "" }, t)))),
          h("tbody", {}, data.items.map(row)))),
      h("nav", { class: "pager", "aria-label": "Paginación" },
        h("span", {}, `${from}–${to} de ${data.total}`),
        h("div", { class: "row" },
          button("", { icon: "chevronLeft", variant: "secondary", size: "sm", disabled: f.page <= 1, "aria-label": "Página anterior",
                       onClick: () => { f.page -= 1; setUrl(); load(); } }),
          h("span", {}, `Página ${f.page} de ${pages}`),
          button("", { icon: "chevronRight", variant: "secondary", size: "sm", disabled: f.page >= pages, "aria-label": "Página siguiente",
                       onClick: () => { f.page += 1; setUrl(); load(); } }))));
  }
  const debouncedLoad = debounce(() => { renderChrome(false); load(); }, 250);

  clear(root,
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Candidatos"), count), h("div", { class: "toolbar" }, exportBtn)),
    viewsEl, filterBar, morePanel, activeEl, results, bulkBar);
  renderChrome();
  await load();
  const onChange = () => load();
  window.addEventListener("bo:candidate-changed", onChange);
  return () => window.removeEventListener("bo:candidate-changed", onChange);
}
