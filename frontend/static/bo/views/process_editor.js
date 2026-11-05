// Process editor.
//
// Basic mode (default) speaks recruiter language: questions are a list, rules
// read as sentences ("Si Mayor de edad es No → descartar"), scoring as "Sumar 20
// puntos si…"; technical identifiers are generated. "Configuración avanzada"
// reveals everything else (ids, data keys, every operator, validation-error
// rules, re-ask messages, keywords, external checks, assistant personality).
// Every edit autosaves to the draft; problems point at the offending item.

import { api } from "../api.js";
import {
  button, chip, clear, debounce, dot, drawer, fmtDateTime, h, icon, iconButton, loc, menu, popover, relTime, scoreBadge,
  segmented, switchInput, toast,
} from "../kit.js";
import { fieldInput, inlineTable } from "../inline.js";
import { loadMeta } from "../shared.js";

const SECTIONS = [
  { id: "general", label: "General", icon: "info" },
  { id: "questions", label: "Preguntas", icon: "list", count: (d) => d.questions.length },
  { id: "rules", label: "Reglas", icon: "rules", count: (d) => d.rules.length },
  { id: "scoring", label: "Puntuación", icon: "trophy", count: (d) => d.scoring.length },
  { id: "checks", label: "Verificaciones externas", icon: "shield", count: (d) => d.checks.length, advanced: true },
  { id: "messages", label: "Mensajes", icon: "message" },
  { id: "faq", label: "Preguntas frecuentes", icon: "book", count: (d, lang) => d.faq.filter((f) => (f.lang || "es") === lang).length },
  { id: "areas", label: "Zonas de trabajo", icon: "map", count: (d) => d.service_areas.length },
  { id: "history", label: "Historial de versiones", icon: "history", sep: true },
];
const SECTION_OF = { questions: "questions", rules: "rules", scoring: "scoring", checks: "checks", faq: "faq",
                     service_areas: "areas", messages: "messages", intro: "messages" };

const TYPE_LABELS = {
  name: "Nombre y apellidos", yes_no: "Sí / No", choice: "Una opción", multi_choice: "Varias opciones", number: "Número",
  experience: "Años de experiencia", date: "Fecha", location: "Ciudad / zona", text: "Texto libre", id_document: "Documento de identidad",
};
const BASIC_OPS = {
  bool: [["eq", "es"]],
  options: [["eq", "es"], ["ne", "no es"]],
  multi: [["contains", "incluye"], ["not_contains", "no incluye"]],
  number: [["lt", "es menor que"], ["gt", "es mayor que"], ["eq", "es igual a"]],
  date: [["within_days", "es en menos de"]],
  check: [["eq", "es"]],
  text: [["missing", "no se responde"], ["present", "se responde"]],
};
const EFFECTS = [["disqualify", "descartar al candidato", "red"], ["review", "enviar a revisión", "amber"], ["flag", "añadir una alerta", "blue"]];
const ERROR_TEXT = { out_of_area: "está fuera de las zonas de trabajo", ambiguous_city: "es una ciudad ambigua" };

const slug = (text) => (text || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32) || "item";

function uniqueId(base, taken) {
  let id = base;
  let n = 2;
  while (taken.includes(id)) id = `${base}_${n++}`;
  return id;
}

function questionKeys(q) {
  switch (q.type) {
    case "experience": return [q.field, q.tags_field].filter(Boolean);
    case "location": return [q.field, q.zone_field || "zone", q.country_field || "country", q.coverage_field || "in_service_area"];
    case "id_document": return [q.field, q.doc_type_field || `${q.field}_type`];
    default: return [q.field];
  }
}

export async function renderProcessEditor(root, id, params) {
  let detail = await api.get(`/api/bo/profiles/${encodeURIComponent(id)}`);
  const schema = detail.schema;
  let doc, base, problems, saveState = "", timer = null, conflict = false;
  let section = SECTIONS.some((s) => s.id === params.get("tab")) ? params.get("tab") : "questions";
  let advanced = localStorage.getItem("bo_advanced") === "1";
  let lang = "es";
  const open = new Set();          // expanded list items (row objects)
  const fresh = new WeakSet();     // rows created in this session: their ids follow their names
  let publishedIds = { questions: [], rules: [] };

  function load(d) {
    detail = d;
    doc = structuredClone(d.working);
    for (const key of ["questions", "rules", "checks", "scoring", "faq", "service_areas"]) doc[key] = doc[key] || [];
    doc.messages = doc.messages || {};
    doc.messages.reengage = doc.messages.reengage || {};
    doc.intro = doc.intro || { es: [""] };
    base = d.has_draft ? d.draft_revision : 0;
    problems = d.problems || [];
    saveState = d.has_draft ? `Borrador guardado ${relTime(d.draft_updated_at)}` : "";
    open.clear();
    const pub = d.published || {};
    publishedIds = { questions: (pub.questions || []).map((q) => q.id), rules: (pub.rules || []).map((r) => r.id) };
  }
  load(detail);

  const head = h("div", { class: "editor-head", testid: "draft-bar" });
  const problemsEl = h("div", { class: "problems", testid: "problems", hidden: true });
  const nav = h("nav", { class: "editor-nav", role: "tablist", "aria-label": "Secciones del proceso" });
  const panel = h("section", { class: "editor-section", role: "tabpanel" });

  // ---------- persistence ----------

  async function saveNow() {
    clearTimeout(timer);
    timer = null;
    if (conflict) return;
    try {
      const res = await api.put(`/api/bo/profiles/${id}/draft`, { profile: doc, base_revision: base });
      base = res.draft_revision;
      problems = res.problems;
      saveState = "Borrador guardado";
    } catch (err) {
      if (err.status === 409) { conflict = true; saveState = "Otra persona ha guardado cambios: recarga para ver su versión."; }
      else saveState = `No se pudo guardar: ${err.message}`;
    }
    renderHead();
    renderProblems();
    renderNav();
    markProblems();
  }

  function changed() {
    saveState = "Guardando…";
    renderHead();
    clearTimeout(timer);
    timer = setTimeout(saveNow, 450);
  }

  function structure() {
    changed();
    setTimeout(() => {
      const active = document.activeElement;
      let restore = null;
      if (active && panel.contains(active) && active.dataset.path) {
        const same = [...panel.querySelectorAll(`[data-path="${CSS.escape(active.dataset.path)}"]`)];
        restore = { path: active.dataset.path, index: same.indexOf(active) };
      }
      renderPanel();
      renderNav();
      if (restore) panel.querySelectorAll(`[data-path="${CSS.escape(restore.path)}"]`)[restore.index]?.focus();
    }, 0);
  }

  async function reload() {
    conflict = false;
    load(await api.get(`/api/bo/profiles/${id}`));
    renderAll();
  }

  // ---------- header ----------

  function renderHead() {
    const hasDraft = base > 0;
    const published = detail.latest_revision > 0;
    const publish = button("Publicar", { icon: "bolt", variant: "primary", testid: "publish",
      disabled: !hasDraft || problems.length > 0 || conflict, title: problems.length ? "Corrige los problemas antes de publicar" : "" });
    publish.addEventListener("click", () => openPublish(publish));
    const more = iconButton("more", "Más acciones del proceso", () => menu(more, [
      { label: "Descartar borrador", icon: "undo", disabled: !(hasDraft && published), onClick: discard },
      { label: "Exportar JSON", icon: "download", disabled: !published, onClick: () => { location.href = `/api/bo/profiles/${id}/versions/${detail.latest_revision}`; } },
      { label: "Historial de versiones", icon: "history", onClick: () => go("history") },
      published && doc.active ? { label: "Abrir chat de candidato", icon: "external", onClick: () => window.open(`/?job=${encodeURIComponent(id)}`, "_blank", "noopener") } : null,
    ], { align: "end" }));
    clear(head,
      h("div", {},
        h("a", { href: "#/processes", class: "back" }, icon("arrowLeft", 14), "Procesos"),
        h("h1", { testid: "process-title" }, loc(doc.role, "es") || id),
        h("div", { class: "state" },
          h("span", {}, doc.company || ""),
          published ? chip(`Publicado r${detail.latest_revision}`, "ok") : chip("Sin publicar", "warn"),
          hasDraft ? chip("Borrador con cambios", "info", { testid: "draft-chip" }) : chip("Sin cambios pendientes", "muted"),
          problems.length ? chip(`${problems.length} problema${problems.length === 1 ? "" : "s"}`, "bad", { testid: "problem-count" }) : null,
          h("span", { class: conflict ? "bad-text" : "", testid: "save-state", "aria-live": "polite" }, saveState),
          conflict ? button("Recargar", { size: "sm", onClick: reload }) : null)),
      h("div", { class: "row" },
        switchInput({ checked: advanced, label: "Configuración avanzada", testid: "advanced-toggle", onChange: (v) => {
          advanced = v;
          localStorage.setItem("bo_advanced", v ? "1" : "0");
          if (!advanced && SECTIONS.find((s) => s.id === section)?.advanced) section = "questions";
          renderNav();
          renderPanel();
        } }),
        h("div", { class: "lang-switch" }, segmented([["es", "ES"], ["en", "EN"]], lang, (v) => { lang = v; renderPanel(); renderNav(); }, "Idioma del contenido")),
        button("Probar", { icon: "flask", testid: "open-simulator", onClick: openSimulator }),
        publish, more));
  }

  function openPublish(anchor) {
    const note = h("input", { placeholder: "Qué cambia (opcional)", "aria-label": "Nota de la publicación" });
    const ok = button("Publicar revisión", { variant: "primary", testid: "confirm-publish", onClick: async () => {
      if (timer) await saveNow();
      try {
        const res = await api.post(`/api/bo/profiles/${id}/publish`, { note: note.value });
        document.querySelector(".popover")?.remove();
        toast(`Publicado: revisión ${res.revision}`);
        await loadMeta(true);
        window.dispatchEvent(new Event("bo:meta-changed"));
        reload();
      } catch (err) {
        if (err.detail?.problems) { problems = err.detail.problems; renderProblems(); markProblems(); }
        toast(err.message, "error");
      }
    } });
    note.addEventListener("keydown", (e) => { if (e.key === "Enter") ok.click(); });
    popover(anchor, h("div", { class: "stack", style: { gap: "10px", padding: "8px" }, role: "dialog", "aria-label": "Publicar cambios" },
      h("div", {}, h("strong", {}, "Publicar cambios"),
        h("p", { class: "muted small", style: { marginTop: "4px" } }, "Los candidatos nuevos usarán esta versión. Los que ya están en conversación terminan con la suya.")),
      note, ok), { align: "end", width: 320 });
  }

  async function discard() {
    await api.del(`/api/bo/profiles/${id}/draft`);
    toast("Borrador descartado");
    reload();
  }

  // ---------- problems ----------

  function renderProblems() {
    problemsEl.hidden = !problems.length;
    if (!problems.length) return;
    clear(problemsEl, h("strong", {}, "Antes de publicar hay que corregir:"),
      h("ul", {}, problems.map((p) => h("li", {}, p.message,
        p.path && p.path.length ? h("button", { class: "jump", onclick: () => jump(p.path) }, "ir →") : null))));
  }

  function findCell(path) {
    for (let n = path.length; n > 0; n -= 1) {
      const el = panel.querySelector(`[data-path="${CSS.escape(path.slice(0, n).join("."))}"]`);
      if (el) return el;
    }
    return null;
  }

  function markProblems() {
    for (const el of panel.querySelectorAll(".has-problem")) { el.classList.remove("has-problem"); el.removeAttribute("title"); }
    for (const p of problems) {
      const el = findCell(p.path || []);
      if (el) { el.classList.add("has-problem"); el.title = p.message; }
    }
  }

  function jump(path) {
    const target = SECTION_OF[path[0]] || "general";
    if (target === "checks" && !advanced) { advanced = true; localStorage.setItem("bo_advanced", "1"); renderHead(); }
    const row = typeof path[1] === "number" ? doc[path[0]]?.[path[1]] : null;
    if (row && path.length > 2) open.add(row);
    go(target);
    const el = findCell(path);
    if (el) { el.scrollIntoView({ block: "center" }); el.focus?.(); }
  }

  // ---------- navigation ----------

  function go(target) {
    section = target;
    history.replaceState(null, "", `#/processes/${encodeURIComponent(id)}?tab=${target}`);
    renderNav();
    renderPanel();
  }

  function renderNav() {
    const bad = new Set(problems.map((p) => SECTION_OF[p.path?.[0]] || "general"));
    clear(nav, SECTIONS.filter((s) => advanced || !s.advanced).map((s) => [
      s.sep ? h("div", { class: "nav-sep" }, "Versiones") : null,
      h("button", { type: "button", role: "tab", "aria-selected": String(s.id === section), testid: `ptab-${s.id}`, onclick: () => go(s.id) },
        icon(s.icon, 16), h("span", {}, s.label),
        bad.has(s.id) ? h("span", { class: "nav-problem", title: "Hay problemas en esta sección" })
          : s.count ? h("span", { class: "count-pill" }, String(s.count(doc, lang))) : null)]));
  }

  // ---------- data dictionary ----------

  const L = (obj) => (obj && obj[lang]) || "";
  const setL = (obj, value) => ({ ...(obj || {}), [lang]: value });
  const ES = (obj) => (obj && (obj.es || obj.en)) || "";

  function dataKeys() {
    const keys = [];
    for (const q of doc.questions) for (const k of questionKeys(q)) if (k && !keys.includes(k)) keys.push(k);
    for (const c of doc.checks) { const k = c.result_field || c.id; if (k && !keys.includes(k)) keys.push(k); }
    return keys;
  }

  function keyLabel(key) {
    const q = doc.questions.find((x) => x.field === key);
    if (q) return ES(q.label) || q.id;
    const c = doc.checks.find((x) => (x.result_field || x.id) === key);
    if (c) return `Verificación: ${ES(c.label) || c.id}`;
    const owner = doc.questions.find((x) => questionKeys(x).includes(key));
    if (owner) {
      const base = ES(owner.label) || owner.id;
      if (owner.type === "location") {
        if (key === (owner.coverage_field || "in_service_area")) return `${base}: dentro de zona`;
        if (key === (owner.zone_field || "zone")) return `${base}: barrio`;
        if (key === (owner.country_field || "country")) return `${base}: país`;
      }
      if (owner.type === "experience" && key === owner.tags_field) return `${base}: detalle (oficios, plataformas…)`;
      if (owner.type === "id_document") return `${base}: tipo de documento`;
    }
    return advanced ? key : null;
  }

  function keyMeta(key) {
    if (doc.checks.some((c) => (c.result_field || c.id) === key)) return { kind: "check" };
    const q = doc.questions.find((x) => questionKeys(x).includes(key));
    if (!q) return { kind: "text" };
    if (q.type === "location" && key === (q.coverage_field || "in_service_area")) return { kind: "bool" };
    if (q.field !== key) return { kind: "text" };
    if (q.type === "yes_no") return { kind: "bool" };
    if (q.type === "choice") return { kind: "options", options: q.options || [] };
    if (q.type === "multi_choice") return { kind: "multi", options: q.options || [] };
    if (q.type === "number" || q.type === "experience") return { kind: "number" };
    if (q.type === "date") return { kind: "date" };
    return { kind: "text" };
  }

  const keyOptions = () => dataKeys().map((k) => [k, keyLabel(k)]).filter(([, l]) => l);

  function opOptions(fieldKey, current) {
    if (advanced) return Object.entries(schema.operators);
    const ops = [...BASIC_OPS[keyMeta(fieldKey).kind] || BASIC_OPS.text];
    if (current && !ops.some(([o]) => o === current)) ops.push([current, schema.operators[current] || current]);
    return ops;
  }

  function valueLabel(fieldKey, value) {
    const meta = keyMeta(fieldKey);
    if (meta.kind === "bool") return value === true ? "Sí" : value === false ? "No" : "—";
    if (meta.kind === "options" || meta.kind === "multi") { const o = meta.options.find((x) => x.value === value); return o ? ES(o.label) : value; }
    if (meta.kind === "check") return { valid: "válido", invalid: "no válido", error: "error", pending: "pendiente" }[value] || value;
    return value ?? "";
  }

  function valueInput(row, fieldKey, path, label = "Valor") {
    const op = row.op || "eq";
    const set = (v) => { row.value = v; autoName(row); changed(); };
    if (op === "missing" || op === "present") return null;
    if (op === "within_days") return [fieldInput("number", row.value, { path, label, onChange: set }), h("span", { class: "kw" }, "días")];
    if (op === "in" || op === "not_in") return fieldInput("tags", Array.isArray(row.value) ? row.value : [], { path, label, placeholder: "valor1, valor2", onChange: set });
    const meta = keyMeta(fieldKey);
    if (meta.kind === "bool") {
      return fieldInput("select", row.value === true ? "true" : row.value === false ? "false" : "", { path, label,
        options: [["", "—"], ["true", "Sí"], ["false", "No"]], onChange: (v) => set(v === "" ? null : v === "true") });
    }
    if (meta.kind === "options" || meta.kind === "multi") {
      return fieldInput("select", row.value ?? "", { path, label, options: [["", "—"], ...meta.options.map((o) => [o.value, ES(o.label) || o.value])],
        onChange: (v) => set(v || null) });
    }
    if (meta.kind === "check") {
      return fieldInput("select", row.value ?? "", { path, label,
        options: [["", "—"], ["valid", "válido"], ["invalid", "no válido"], ["error", "error"], ["pending", "pendiente"]], onChange: (v) => set(v || null) });
    }
    if (meta.kind === "number") return fieldInput("number", row.value, { path, label, onChange: set });
    return fieldInput("text", row.value ?? "", { path, label, onChange: set });
  }

  // names/ids of rows created here follow what the recruiter picks
  function autoName(row) {
    if (!fresh.has(row)) return;
    if (doc.rules.includes(row)) {
      const cond = row.on_error ? `${keyLabel(doc.questions.find((q) => q.id === row.question)?.field) || row.question} fuera de zona`
        : `${keyLabel(row.field) || row.field} ${(schema.operators[row.op] || row.op)} ${valueLabel(row.field, row.value)}`.trim();
      row.label = { es: cond, en: row.label?.en || cond };
      row.id = uniqueId(slug(cond), doc.rules.filter((r) => r !== row).map((r) => r.id));
    } else if (doc.scoring.includes(row)) {
      const text = row.per_unit != null ? `${keyLabel(row.field) || row.field} (por unidad)` : `${keyLabel(row.field) || row.field} ${schema.operators[row.op] || row.op} ${valueLabel(row.field, row.value)}`;
      row.label = { es: text, en: row.label?.en || text };
    }
  }

  // ---------- small building blocks ----------

  function ghostText(value, { path, label, onChange, multiline = false, cls = "", placeholder = "" }) {
    const el = multiline ? h("textarea", { class: `ghost-input text ${cls}`, rows: 1, "aria-label": label, placeholder })
      : h("input", { class: `ghost-input ${cls}`, "aria-label": label, placeholder });
    el.value = value ?? "";
    if (path) el.dataset.path = path;
    const fit = () => { if (multiline) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; } };
    el.addEventListener("input", fit);
    el.addEventListener("change", () => onChange(el.value));
    requestAnimationFrame(fit);
    return el;
  }

  function settingsRow(label, hint, control) {
    return h("div", { class: "settings-row" }, h("div", {}, h("div", { class: "strong" }, label), hint ? h("div", { class: "hint" }, hint) : null), control);
  }

  function listItem(sectionKey, rows, row, i, { main, side = [], details, menuItems = [], label }) {
    const isOpen = open.has(row);
    const toggle = details ? iconButton(isOpen ? "chevronDown" : "chevronRight", `${isOpen ? "Ocultar" : "Ver"} detalles de ${label}`,
      () => { isOpen ? open.delete(row) : open.add(row); renderPanel(); }) : null;
    if (toggle) toggle.setAttribute("aria-expanded", String(isOpen));
    const more = iconButton("more", `Opciones de ${label}`, () => menu(more, [
      { label: "Mover arriba", icon: "arrowUp", disabled: i === 0, onClick: () => { rows.splice(i - 1, 0, rows.splice(i, 1)[0]); structure(); } },
      { label: "Mover abajo", icon: "arrowDown", disabled: i === rows.length - 1, onClick: () => { rows.splice(i + 1, 0, rows.splice(i, 1)[0]); structure(); } },
      ...menuItems,
      "separator",
      { label: "Eliminar", icon: "trash", danger: true, testid: "delete-item", onClick: () => { rows.splice(i, 1); open.delete(row); structure(); } },
    ], { align: "end" }));
    const item = h("div", { class: "item", dataset: { path: `${sectionKey}.${i}` }, testid: `item-${sectionKey}`, "aria-label": label },
      h("div", { class: "item-row" },
        h("span", { class: "item-grip", title: "Arrastra para reordenar" }, icon("grip", 16)),
        h("span", { class: "item-index" }, String(i + 1)),
        h("div", { class: "item-main" }, main),
        h("div", { class: "item-side" }, side, toggle, more)),
      details && isOpen ? h("div", { class: "item-details" }, details()) : null);
    // drag & drop reordering (grip)
    const grip = item.querySelector(".item-grip");
    grip.addEventListener("mousedown", () => { item.draggable = true; });
    item.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/item", String(i)); item.classList.add("dragging"); });
    item.addEventListener("dragend", () => { item.draggable = false; item.classList.remove("dragging"); });
    item.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("text/item")) { e.preventDefault(); item.classList.add("drop-before"); } });
    item.addEventListener("dragleave", () => item.classList.remove("drop-before"));
    item.addEventListener("drop", (e) => {
      e.preventDefault();
      item.classList.remove("drop-before");
      const from = Number(e.dataTransfer.getData("text/item"));
      if (Number.isNaN(from) || from === i) return;
      const [moved] = rows.splice(from, 1);
      rows.splice(from < i ? i - 1 : i, 0, moved);
      structure();
    });
    return item;
  }

  // ---------- sections ----------

  function sectionHead(title, sub, ...actions) {
    return h("div", { class: "section-title" }, h("div", {}, h("h3", {}, title), sub ? h("p", { class: "muted small" }, sub) : null),
      actions.length ? h("div", { class: "row" }, actions) : null);
  }

  function general() {
    const persona = () => (doc.persona = doc.persona || {});
    const input = (value, path, label, set, type = "text", opts = {}) => fieldInput(type, value, { path, label, onChange: (v) => { set(v); changed(); }, ...opts });
    return [
      sectionHead("General", "Lo que ve el candidato al elegir la oferta y cómo se presenta el asistente."),
      h("div", { class: "panel-card settings", testid: "table-general" },
        settingsRow("Puesto", null, input(L(doc.role), "role", "Puesto", (v) => { doc.role = setL(doc.role, v); })),
        settingsRow("Empresa", null, input(doc.company, "company", "Empresa", (v) => { doc.company = v; })),
        settingsRow("Descripción de la oferta", "Se muestra al candidato al elegir oferta", input(L(doc.summary), "summary", "Descripción de la oferta",
          (v) => { doc.summary = setL(doc.summary, v); }, "textarea", { rows: 2 })),
        settingsRow("Nombre del asistente", null, input(persona().agent_name, "persona.agent_name", "Nombre del asistente", (v) => { persona().agent_name = v; })),
        settingsRow("Visible para candidatos", "Aparece en la lista de ofertas al publicar",
          switchInput({ checked: doc.active !== false, label: doc.active !== false ? "Activo" : "Inactivo", onChange: (v) => { doc.active = v; structure(); } })),
        advanced ? [
          settingsRow("Personalidad del asistente", "Instrucciones de tono para el modelo (en inglés)", input(persona().personality, "persona.personality",
            "Personalidad del asistente", (v) => { persona().personality = v; }, "textarea", { rows: 3 })),
          settingsRow("Contexto", "Dónde se trabaja, qué empresa…", input(persona().context, "persona.context", "Contexto", (v) => { persona().context = v; })),
          settingsRow("Idioma por defecto", null, input(doc.default_language, "default_language", "Idioma por defecto", (v) => { doc.default_language = v; },
            "select", { options: [["es", "Español"], ["en", "English"]] })),
          settingsRow("Etiqueta de versión", null, input(doc.version, "version", "Etiqueta de versión", (v) => { doc.version = v; })),
          settingsRow("Intentos por pregunta", "Re-preguntas antes de aceptar la respuesta con una alerta", input(doc.max_stage_attempts, "max_stage_attempts",
            "Intentos por pregunta", (v) => { doc.max_stage_attempts = v; }, "number")),
        ] : null),
    ];
  }

  function optionChips(q, i) {
    q.options = q.options || [];
    return h("div", { class: "field" }, h("span", {}, "Opciones de respuesta"),
      h("div", { class: "option-chips", testid: `options-${q.id}` },
        q.options.map((o, j) => {
          const input = h("input", { value: L(o.label), "aria-label": `Opción ${j + 1} de ${ES(q.label)}`, size: Math.max(6, L(o.label).length + 1) });
          input.dataset.path = `questions.${i}.options.${j}`;
          input.addEventListener("change", () => {
            o.label = setL(o.label, input.value);
            if (fresh.has(o) && lang === "es") o.value = uniqueId(slug(input.value), q.options.filter((x) => x !== o).map((x) => x.value));
            changed();
          });
          return h("span", { class: "option-chip" }, input,
            iconButton("x", `Quitar opción ${L(o.label) || j + 1}`, () => { q.options.splice(j, 1); structure(); }));
        }),
        button("Añadir opción", { icon: "plus", size: "sm", variant: "ghost", onClick: () => {
          const o = { value: uniqueId("opcion", q.options.map((x) => x.value)), label: { es: "Nueva opción", en: "New option" }, keywords: [] };
          fresh.add(o);
          q.options.push(o);
          structure();
          setTimeout(() => panel.querySelector(`[data-path="questions.${i}.options.${q.options.length - 1}"]`)?.select(), 30);
        } })),
      h("span", { class: "hint" }, "El asistente reconoce la opción aunque el candidato la diga con otras palabras."));
  }

  function questionDetails(q, i) {
    const p = (k) => `questions.${i}.${k}`;
    const parts = [];
    if ((q.type === "choice" || q.type === "multi_choice") && !advanced) parts.push(optionChips(q, i));
    if (q.type === "location") parts.push(h("p", { class: "hint" }, icon("map", 14), " La ciudad se comprueba contra las Zonas de trabajo del proceso."));
    if (q.type === "id_document") {
      parts.push(h("fieldset", { class: "checks-field" }, h("legend", {}, "Documentos aceptados"),
        Object.entries(schema.documents).map(([value, label]) => {
          const box = h("input", { type: "checkbox", checked: (q.documents || ["dni", "nie"]).includes(value) });
          box.addEventListener("change", () => {
            const set = new Set(q.documents || ["dni", "nie"]);
            box.checked ? set.add(value) : set.delete(value);
            q.documents = Object.keys(schema.documents).filter((d) => set.has(d));
            changed();
          });
          return h("label", { class: "check" }, box, label);
        })),
        h("p", { class: "hint" }, "Se valida sin pasar por el modelo de lenguaje y se guarda enmascarado."));
    }
    if (advanced) {
      const txt = (key, label, opts = {}) => h("label", {}, label, fieldInput("text", q[key] ?? "", { label, path: p(key), onChange: (v) => { q[key] = v || null; key === "id" || key === "field" ? structure() : changed(); }, ...opts }));
      const num = (key, label) => h("label", {}, label, fieldInput("number", q[key], { label, path: p(key), onChange: (v) => { q[key] = v; changed(); } }));
      const variants = h("label", { class: "span-2" }, `Otras formas de preguntar (${lang.toUpperCase()}, una por línea)`,
        fieldInput("textarea", ((q.prompts || {})[lang] || []).slice(1).join("\n"), { label: "Otras formas de preguntar", path: p("prompts"),
          onChange: (v) => { q.prompts = q.prompts || {}; q.prompts[lang] = [((q.prompts[lang] || [])[0] || ""), ...v.split("\n").map((x) => x.trim()).filter(Boolean)]; changed(); } }));
      const grid = [txt("id", "ID (etapa)"), txt("field", "Dato guardado"), h("label", { class: "span-2" }, "Descripción para el modelo",
        fieldInput("text", q.description || "", { label: "Descripción para el modelo", path: p("description"), onChange: (v) => { q.description = v; changed(); } })), variants];
      if (q.type === "yes_no") grid.push(num("number_threshold", "Umbral numérico (18 → «tengo 17» es No)"));
      if (q.type === "number" || q.type === "experience") grid.push(num("min", "Mínimo"), num("max", "Máximo"));
      if (q.type === "date") grid.push(num("max_days_ahead", "Días máximos hasta la incorporación"));
      if (q.type === "experience") {
        grid.push(txt("tags_field", "Dato para los detalles (oficios, plataformas…)"));
        grid.push(h("label", { class: "span-2" }, "Vocabulario («término: sinónimo, sinónimo» por línea)",
          fieldInput("textarea", Object.entries(q.tags || {}).map(([t, a]) => `${t}: ${a.join(", ")}`).join("\n"), {
            label: "Vocabulario", rows: 4, path: p("tags"),
            onChange: (v) => {
              const tags = {};
              for (const line of v.split("\n")) { const [term, aliases = ""] = line.split(":"); if (term.trim()) tags[term.trim()] = aliases.split(",").map((a) => a.trim()).filter(Boolean); }
              q.tags = tags; changed();
            } })));
      }
      parts.push(h("div", { class: "detail-grid" }, grid));
      if (q.type === "choice" || q.type === "multi_choice") {
        parts.push(inlineTable({
          section: `questions.${i}.options`, rows: q.options, onChange: changed, onStructure: structure, testid: `options-table-${q.id}`,
          rowLabel: (o, j) => `opción ${ES(o.label) || j + 1}`,
          columns: [
            { label: "Valor guardado", key: "value", width: "150px" },
            { label: `Texto (${lang.toUpperCase()})`, pathKey: "label", get: (o) => L(o.label), set: (o, v) => { o.label = setL(o.label, v); } },
            { label: "Sinónimos que reconoce", key: "keywords", type: "tags", placeholder: "separados por comas", class: "wide" },
          ],
        }));
      }
      const codes = schema.errors[q.type] || [];
      if (codes.length) {
        q.reask = q.reask || {};
        parts.push(h("div", { class: "field" }, h("span", {}, `Mensajes cuando la respuesta no vale (${lang.toUpperCase()})`),
          h("div", { class: "stack", style: { gap: "6px" } }, codes.map((code) => h("div", { class: "row" }, h("code", { style: { minWidth: "150px" } }, code),
            h("div", { class: "grow" }, fieldInput("text", (q.reask[code] || {})[lang] || "", {
              label: `Mensaje ${code}`, placeholder: "mensaje por defecto", path: p(`reask.${code}`),
              onChange: (v) => {
                const msg = { ...(q.reask[code] || {}) };
                if (v) msg[lang] = v; else delete msg[lang];
                if (Object.keys(msg).length) q.reask[code] = msg; else delete q.reask[code];
                changed();
              } })))))));
      }
    }
    return parts.length ? parts : [h("p", { class: "hint" }, "Este tipo de respuesta no tiene más ajustes.")];
  }

  function questions() {
    const hasDetails = (q) => advanced || ["choice", "multi_choice", "location", "id_document"].includes(q.type);
    return [
      sectionHead("Preguntas", "Lo que pregunta el asistente, en este orden. Arrastra para reordenar.",
        button("Añadir pregunta", { icon: "plus", testid: "add-question", onClick: () => {
          const q = { id: "", field: "", type: "text", required: true, label: { es: "Nueva pregunta", en: "New question" },
                      description: "", prompts: { es: ["¿…?"], en: ["…?"] } };
          q.id = q.field = uniqueId("nueva_pregunta", doc.questions.map((x) => x.id).concat(doc.questions.map((x) => x.field)));
          fresh.add(q);
          doc.questions.push(q);
          structure();
          setTimeout(() => panel.querySelector(`[data-path="questions.${doc.questions.length - 1}.label"]`)?.select(), 30);
        } })),
      h("div", { class: "item-list", testid: "table-questions" }, doc.questions.map((q, i) => {
        const label = ES(q.label) || q.id;
        const title = ghostText(L(q.label), { path: `questions.${i}.label`, label: `Nombre de la pregunta ${label}`, cls: "title", placeholder: "Nombre corto",
          onChange: (v) => {
            q.label = setL(q.label, v);
            if (fresh.has(q) && lang === "es" && !publishedIds.questions.includes(q.id)) {
              const taken = doc.questions.filter((x) => x !== q).flatMap((x) => [x.id, x.field]);
              q.id = q.field = uniqueId(slug(v), taken);
            }
            structure();
          } });
        const text = ghostText(((q.prompts || {})[lang] || [])[0] || "", { path: `questions.${i}.prompts`, label: `Texto de la pregunta ${label}`, multiline: true,
          placeholder: "Lo que pregunta el asistente",
          onChange: (v) => { q.prompts = q.prompts || {}; const arr = [...(q.prompts[lang] || [])]; arr[0] = v; q.prompts[lang] = arr; changed(); } });
        const type = fieldInput("select", q.type, { label: `Tipo de respuesta de ${label}`, path: `questions.${i}.type`,
          options: Object.entries(TYPE_LABELS), onChange: (v) => { q.type = v; if ((v === "choice" || v === "multi_choice") && !(q.options || []).length) { q.options = []; open.add(q); } structure(); } });
        type.classList.add("type-select");
        const required = switchInput({ checked: q.required !== false, label: "Obligatoria", onChange: (v) => { q.required = v; changed(); } });
        required.querySelector("input").setAttribute("aria-label", `Obligatoria: ${label}`);
        return listItem("questions", doc.questions, q, i, {
          label: `pregunta ${label}`,
          main: [title, text, advanced ? h("span", { class: "muted small" }, `id ${q.id} · dato ${q.field}`) : null],
          side: [type, required],
          details: hasDetails(q) ? () => questionDetails(q, i) : null,
          menuItems: [{ label: "Duplicar", icon: "copy", onClick: () => {
            const copy = structuredClone(q);
            const taken = doc.questions.flatMap((x) => [x.id, x.field]);
            copy.id = copy.field = uniqueId(`${q.id}_copia`, taken);
            copy.label = { ...q.label, es: `${ES(q.label)} (copia)` };
            fresh.add(copy);
            doc.questions.splice(i + 1, 0, copy);
            structure();
          } }],
        });
      })),
    ];
  }

  function effectOf(r) {
    return r.action === "disqualify" ? "disqualify" : r.review ? "review" : "flag";
  }

  function ruleItem(r, i) {
    const label = ES(r.label) || r.id;
    const effect = fieldInput("select", effectOf(r), { label: `Efecto de la regla ${label}`, path: `rules.${i}.action`,
      options: EFFECTS.map(([v, l]) => [v, l]), onChange: (v) => {
        r.action = v === "disqualify" ? "disqualify" : "flag";
        if (v === "review") r.review = true; else delete r.review;
        if (v !== "disqualify" && !(r.flags || []).length) r.flags = [r.id];
        structure();
      } });
    effect.classList.add("effect", `tone-${EFFECTS.find(([v]) => v === effectOf(r))[2]}`);
    let condition;
    if (r.on_error) {
      const q = doc.questions.find((x) => x.id === r.question);
      condition = advanced
        ? [fieldInput("select", r.question, { label: "Pregunta", path: `rules.${i}.question`, options: doc.questions.map((x) => [x.id, ES(x.label) || x.id]), onChange: (v) => { r.question = v; structure(); } }),
           h("span", { class: "kw" }, "da el error"),
           fieldInput("select", r.on_error, { label: "Error", path: `rules.${i}.on_error`, options: (schema.errors[q?.type] || [r.on_error]).map((e) => [e, e]), onChange: (v) => { r.on_error = v; changed(); } }),
           h("span", { class: "kw" }, "tras"),
           fieldInput("number", r.after_attempts ?? 0, { label: "Reintentos", path: `rules.${i}.after_attempts`, onChange: (v) => { r.after_attempts = v ?? 0; changed(); } }),
           h("span", { class: "kw" }, "reintento(s)")]
        : [h("strong", {}, ES(q?.label) || r.question), h("span", {}, ERROR_TEXT[r.on_error] || `da el error «${r.on_error}»`),
           r.after_attempts ? h("span", { class: "kw" }, `(tras ${r.after_attempts} reintento${r.after_attempts === 1 ? "" : "s"})`) : null,
           h("span", { class: "locked", title: "Se edita en la configuración avanzada" }, icon("lock", 13))];
    } else {
      const fieldSel = fieldInput("select", r.field ?? "", { label: `Dato de la regla ${label}`, path: `rules.${i}.field`,
        options: keyOptions(), onChange: (v) => { r.field = v; r.op = opOptions(v)[0][0]; r.value = null; autoName(r); structure(); } });
      const opSel = fieldInput("select", r.op || "eq", { label: `Condición de la regla ${label}`, path: `rules.${i}.op`,
        options: opOptions(r.field, r.op), onChange: (v) => { r.op = v; autoName(r); structure(); } });
      opSel.classList.add("op");
      condition = [fieldSel, opSel, valueInput(r, r.field, `rules.${i}.value`, `Valor de la regla ${label}`)];
    }
    const main = [
      h("div", { class: "sentence" }, h("span", { class: "kw" }, "Si"), condition, icon("arrowRight", 15), effect),
      r.action === "disqualify"
        ? ghostText(L(r.message), { path: `rules.${i}.message`, label: `Mensaje al candidato de la regla ${label}`, multiline: true,
            placeholder: "Mensaje al candidato: explícale con respeto por qué no puede seguir…", onChange: (v) => { r.message = setL(r.message, v); changed(); } })
        : ghostText(r.note || "", { path: `rules.${i}.note`, label: `Nota para el reclutador de la regla ${label}`, multiline: true,
            placeholder: "Nota para el reclutador (opcional)", onChange: (v) => { r.note = v || null; changed(); } }),
    ];
    return listItem("rules", doc.rules, r, i, {
      label: `regla ${label}`, main,
      details: advanced ? () => [h("div", { class: "detail-grid" },
        h("label", {}, "ID", fieldInput("text", r.id, { label: "ID de la regla", path: `rules.${i}.id`, onChange: (v) => { r.id = v; structure(); } })),
        h("label", {}, `Nombre (${lang.toUpperCase()})`, fieldInput("text", L(r.label), { label: "Nombre de la regla", path: `rules.${i}.label`, onChange: (v) => { r.label = setL(r.label, v); changed(); } })),
        h("label", {}, "Alertas que añade (separadas por comas)", fieldInput("tags", r.flags || [], { label: "Alertas", path: `rules.${i}.flags`, onChange: (v) => { r.flags = v; changed(); } })),
        h("label", {}, "Se dispara por", fieldInput("select", r.on_error ? "error" : "field", { label: "Se dispara por", options: [["field", "Un dato"], ["error", "Un error al validar"]],
          onChange: (v) => {
            if (v === "error") {
              const q = doc.questions.find((x) => x.type === "location") || doc.questions[0];
              delete r.field; delete r.op; delete r.value;
              r.question = q?.id; r.on_error = (schema.errors[q?.type] || ["empty"]).slice(-1)[0]; r.after_attempts = 1;
            } else {
              delete r.on_error; delete r.question; delete r.after_attempts;
              r.field = dataKeys()[0]; r.op = "eq"; r.value = null;
            }
            structure();
          } })))] : null,
    });
  }

  function rules() {
    return [
      sectionHead("Reglas", "Qué hace el asistente según las respuestas: descartar, enviar a revisión o avisar con una alerta.",
        button("Añadir regla", { icon: "plus", testid: "add-rule", onClick: () => {
          const field = dataKeys().find((k) => keyMeta(k).kind === "bool") || dataKeys()[0];
          const r = { id: "", action: "disqualify", label: { es: "", en: "" }, field, op: opOptions(field)[0][0], value: null,
                      flags: [], message: { es: "", en: "" } };
          fresh.add(r);
          doc.rules.push(r);
          autoName(r);
          structure();
        } })),
      h("div", { class: "item-list", testid: "table-rules" }, doc.rules.map(ruleItem)),
      doc.rules.length ? null : h("div", { class: "callout" }, icon("info", 16), h("span", {}, "Sin reglas: todos los candidatos que respondan se pre-califican.")),
    ];
  }

  function scoring() {
    const ceilings = {};
    for (const s of doc.scoring) {
      const c = s.per_unit != null ? Number(s.max_points || 0) : Number(s.points || 0);
      ceilings[s.field] = Math.max(ceilings[s.field] || 0, c);
    }
    const total = Object.values(ceilings).reduce((a, b) => a + b, 0);
    return [
      sectionHead("Puntuación", "Ordena a los candidatos de 0 a 100. No decide quién pasa: eso lo hacen las reglas.",
        button("Añadir criterio", { icon: "plus", testid: "add-criterion", onClick: () => {
          const field = dataKeys()[0];
          const s = { field, label: { es: "", en: "" }, op: opOptions(field)[0][0], value: null, points: 10 };
          fresh.add(s);
          doc.scoring.push(s);
          autoName(s);
          structure();
        } })),
      h("div", { class: "item-list", testid: "table-scoring" }, doc.scoring.map((s, i) => {
        const label = ES(s.label) || s.field;
        const perUnit = s.per_unit != null;
        const points = fieldInput("number", perUnit ? s.per_unit : s.points, { label: `Puntos del criterio ${label}`, path: `scoring.${i}.${perUnit ? "per_unit" : "points"}`,
          onChange: (v) => { if (perUnit) s.per_unit = v; else s.points = v; structure(); } });
        const fieldSel = fieldInput("select", s.field, { label: `Dato del criterio ${label}`, path: `scoring.${i}.field`, options: keyOptions(),
          onChange: (v) => { s.field = v; if (!perUnit) { s.op = opOptions(v)[0][0]; s.value = null; } autoName(s); structure(); } });
        const sentence = perUnit
          ? [h("span", { class: "kw" }, "Sumar"), points, h("span", { class: "kw" }, "puntos por cada unidad de"), fieldSel,
             h("span", { class: "kw" }, "hasta"), fieldInput("number", s.max_points, { label: `Máximo del criterio ${label}`, path: `scoring.${i}.max_points`,
               onChange: (v) => { s.max_points = v; structure(); } }), h("span", { class: "kw" }, "puntos")]
          : [h("span", { class: "kw" }, "Sumar"), points, h("span", { class: "kw" }, "puntos si"), fieldSel,
             (() => { const o = fieldInput("select", s.op || "eq", { label: `Condición del criterio ${label}`, path: `scoring.${i}.op`, options: opOptions(s.field, s.op),
               onChange: (v) => { s.op = v; autoName(s); structure(); } }); o.classList.add("op"); return o; })(),
             valueInput(s, s.field, `scoring.${i}.value`, `Valor del criterio ${label}`)];
        return listItem("scoring", doc.scoring, s, i, {
          label: `criterio ${label}`,
          main: [h("div", { class: "sentence" }, sentence), advanced ? ghostText(L(s.label), { path: `scoring.${i}.label`, label: "Nombre del criterio",
            placeholder: "Nombre del criterio", onChange: (v) => { s.label = setL(s.label, v); changed(); } }) : null],
          menuItems: [{ label: perUnit ? "Cambiar a «si se cumple»" : "Cambiar a «por unidad»", icon: "refresh", onClick: () => {
            if (perUnit) { s.op = opOptions(s.field)[0][0]; s.value = null; s.points = s.max_points || 10; delete s.per_unit; delete s.max_points; }
            else { s.per_unit = 1; s.max_points = s.points || 10; delete s.op; delete s.value; s.points = 0; }
            autoName(s);
            structure();
          } }],
        });
      })),
      h("div", { class: "score-total", testid: "score-total" }, icon("trophy", 16),
        h("span", {}, `Máximo alcanzable: ${total} puntos → se normaliza a 100.`),
        h("span", { class: "muted small" }, "Los criterios sobre el mismo dato son alternativos (cuenta el mayor).")),
    ];
  }

  function checkDetails(c, i) {
    const p = (k) => `checks.${i}.${k}`;
    const whenBox = h("input", { type: "checkbox", checked: !!c.when, "aria-label": "Solo si se cumple una condición" });
    whenBox.addEventListener("change", () => { c.when = whenBox.checked ? { field: dataKeys()[0], op: "eq", value: null } : null; structure(); });
    const sensitive = doc.questions.filter((q) => q.type === "id_document").map((q) => q.field);
    return h("div", { class: "details stack", style: { gap: "12px" } },
      h("fieldset", { class: "checks-field" }, h("legend", {}, "Datos que se envían"),
        dataKeys().filter((k) => !doc.checks.some((x) => (x.result_field || x.id) === k)).map((k) => {
          const box = h("input", { type: "checkbox", checked: (c.inputs || []).includes(k) });
          box.dataset.path = p("inputs");
          box.addEventListener("change", () => {
            const set = new Set(c.inputs || []);
            box.checked ? set.add(k) : set.delete(k);
            c.inputs = dataKeys().filter((x) => set.has(x));
            changed();
          });
          return h("label", { class: "check" }, box, keyLabel(k) || k, sensitive.includes(k) ? chip("sensible", "warn") : null);
        })),
      h("div", { class: "sentence" }, h("label", { class: "check" }, whenBox, "Solo si"),
        c.when ? [
          fieldInput("select", c.when.field, { label: "Dato de la condición", path: p("when"), options: keyOptions(), onChange: (v) => { c.when.field = v; c.when.value = null; structure(); } }),
          fieldInput("select", c.when.op || "eq", { label: "Operador de la condición", options: Object.entries(schema.operators), onChange: (v) => { c.when.op = v; structure(); } }),
          valueInput(c.when, c.when.field, p("when"), "Valor de la condición"),
        ] : h("span", { class: "muted small" }, "(siempre que se responda la pregunta)")),
      h("div", { class: "detail-grid" },
        c.provider === "webhook" ? h("label", { class: "span-2" }, "URL del servicio (vacío = VERIFICATION_WEBHOOK_URL)",
          fieldInput("text", c.url || "", { label: "URL del servicio", path: p("url"), onChange: (v) => { c.url = v || null; changed(); } })) : null,
        h("label", {}, "Dato donde se guarda el resultado", fieldInput("text", c.result_field || "", { label: "Dato del resultado", placeholder: c.id,
          path: p("result_field"), onChange: (v) => { c.result_field = v || null; structure(); } })),
        h("label", {}, `Nombre (${lang.toUpperCase()})`, fieldInput("text", L(c.label), { label: "Nombre de la verificación", path: p("label"),
          onChange: (v) => { c.label = setL(c.label, v); changed(); } }))),
      h("p", { class: "hint" }, `El resultado (pendiente → válido / no válido / error) se guarda en «${c.result_field || c.id}»: añade una regla que lo use, p. ej. «es no válido → descartar».`));
  }

  function checks() {
    return [
      sectionHead("Verificaciones externas", "Validaciones asíncronas de un sistema externo (p. ej. registro de identidad). La conversación no espera; si es bloqueante, la decisión final queda «En verificación»."),
      inlineTable({
        section: "checks", rows: doc.checks, onChange: changed, onStructure: structure, details: checkDetails, openRows: open,
        rowLabel: (c, i) => `verificación ${c.id || i + 1}`,
        columns: [
          { label: "ID", key: "id", width: "140px", structural: true },
          { label: "Nombre", pathKey: "label", get: (c) => L(c.label), set: (c, v) => { c.label = setL(c.label, v); } },
          { label: "Proveedor", key: "provider", type: "select", options: Object.entries(schema.providers), structural: true },
          { label: "Se lanza al responder", key: "trigger", type: "select",
            options: () => [...doc.questions.map((q) => [q.id, ES(q.label) || q.id]), [schema.on_complete, "Al terminar el screening"]] },
          { label: "Bloqueante", key: "blocking", type: "checkbox", class: "center", width: "90px" },
          { label: "Plazo (s)", key: "timeout_seconds", type: "number", width: "100px" },
        ],
        newRow: () => ({ id: uniqueId("verificacion", doc.checks.map((c) => c.id)), label: { es: "Nueva verificación", en: "New check" },
                         provider: "manual", trigger: schema.on_complete, inputs: [], blocking: false, timeout_seconds: 86400 }),
        addLabel: "Añadir verificación",
      }),
    ];
  }

  function messages() {
    const cards = [
      ["Saludo inicial", "Primer mensaje del asistente. {name} = asistente, {company}, {role}.", () => (doc.intro[lang] || [""])[0],
        (v) => { doc.intro[lang] = [v, ...((doc.intro[lang] || []).slice(1))]; }, "intro"],
      ...Object.entries(schema.messages)
        .filter(([key]) => advanced || ["qualified", "review", "verifying"].includes(key))
        .filter(([key]) => key !== "verifying" || doc.checks.length || advanced)
        .map(([key, label]) => [label, null, () => L(doc.messages[key]), (v) => { doc.messages[key] = setL(doc.messages[key], v); }, `messages.${key}`]),
      ...(advanced ? [["Recordatorio 1", "Si el candidato deja de responder", () => L(doc.messages.reengage["1"]), (v) => { doc.messages.reengage["1"] = setL(doc.messages.reengage["1"], v); }, "messages.reengage"],
                      ["Recordatorio 2", null, () => L(doc.messages.reengage["2"]), (v) => { doc.messages.reengage["2"] = setL(doc.messages.reengage["2"], v); }, "messages.reengage"]] : []),
    ];
    return [
      sectionHead("Mensajes", "Lo que dice el asistente al empezar y al terminar. Los mensajes de descarte están en cada regla."),
      h("div", { class: "msg-cards", testid: "table-messages" }, cards.map(([label, hint, get, set, path]) => h("div", { class: "msg-card" },
        h("div", {}, h("strong", {}, label), hint ? h("div", { class: "hint" }, hint) : null),
        fieldInput("textarea", get(), { label, path, rows: 2, onChange: (v) => { set(v); changed(); } })))),
    ];
  }

  function faq() {
    const items = doc.faq.map((f, i) => [f, i]).filter(([f]) => (f.lang || "es") === lang);
    return [
      sectionHead("Preguntas frecuentes", `El asistente solo responde con esta información (${lang === "es" ? "español" : "inglés"}).`,
        button("Añadir pregunta frecuente", { icon: "plus", onClick: () => {
          doc.faq.push({ id: uniqueId(`faq_${lang}`, doc.faq.map((f) => f.id)), lang, topic: "", q: "", a: "" });
          structure();
        } })),
      h("div", { class: "item-list", testid: "table-faq" }, items.length ? items.map(([f, i]) => h("div", { class: "item", dataset: { path: `faq.${i}` } },
        h("div", { class: "item-row" }, h("span", { class: "item-index" }, icon("book", 13)),
          h("div", { class: "item-main" },
            ghostText(f.q, { path: `faq.${i}.q`, label: "Pregunta frecuente", cls: "title", placeholder: "Pregunta (p. ej. ¿Cuánto se cobra?)", onChange: (v) => { f.q = v; changed(); } }),
            ghostText(f.a, { path: `faq.${i}.a`, label: "Respuesta", multiline: true, placeholder: "Respuesta", onChange: (v) => { f.a = v; changed(); } }),
            advanced ? h("span", { class: "muted small" }, `id ${f.id}`) : null),
          h("div", { class: "item-side" }, iconButton("trash", "Eliminar pregunta frecuente", () => { doc.faq.splice(i, 1); structure(); })))))
        : h("div", { class: "callout" }, icon("info", 16), h("span", {}, "No hay preguntas frecuentes en este idioma."))),
    ];
  }

  function areas() {
    return [
      sectionHead("Zonas de trabajo", "Dónde se trabaja. Sin zonas, la pregunta de ubicación acepta cualquier ciudad.",
        button("Añadir ciudad", { icon: "plus", onClick: () => { doc.service_areas.push({ city: "", country: "ES", aliases: [], zones: [] }); structure(); } })),
      advanced
        ? inlineTable({
            section: "service_areas", rows: doc.service_areas, onChange: changed, onStructure: structure, rowLabel: (a, i) => `zona ${a.city || i + 1}`,
            columns: [
              { label: "Ciudad", key: "city", width: "170px" }, { label: "País", key: "country", width: "80px" },
              { label: "Otros nombres", key: "aliases", type: "tags" }, { label: "Barrios / municipios", key: "zones", type: "tags", class: "wide" },
            ] })
        : h("div", { class: "item-list", testid: "table-areas" }, doc.service_areas.map((a, i) => h("div", { class: "item", dataset: { path: `service_areas.${i}` } },
            h("div", { class: "item-row" }, h("span", { class: "item-index" }, icon("pin", 13)),
              h("div", { class: "item-main" },
                ghostText(a.city, { path: `service_areas.${i}.city`, label: "Ciudad", cls: "title", placeholder: "Ciudad", onChange: (v) => { a.city = v; changed(); } }),
                ghostText((a.zones || []).join(", "), { path: `service_areas.${i}.zones`, label: `Barrios de ${a.city}`, placeholder: "Barrios o municipios cercanos, separados por comas",
                  onChange: (v) => { a.zones = v.split(",").map((x) => x.trim()).filter(Boolean); changed(); } })),
              h("div", { class: "item-side" }, iconButton("trash", `Eliminar ${a.city || "ciudad"}`, () => { doc.service_areas.splice(i, 1); structure(); })))))),
    ];
  }

  function versions() {
    return [
      sectionHead("Historial de versiones", "Cada publicación es una versión. Los candidatos quedan ligados a la versión con la que empezaron."),
      h("div", { class: "table-wrap" }, h("table", { class: "table", testid: "versions" },
        h("thead", {}, h("tr", {}, ["Versión", "Publicada", "Por", "Nota", "Candidatos", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, (detail.versions || []).map((v) => h("tr", {},
          h("td", {}, h("strong", {}, `r${v.revision}`), v.revision === detail.latest_revision ? [" ", chip("actual", "ok")] : null),
          h("td", {}, fmtDateTime(v.published_at)), h("td", {}, v.published_by), h("td", { class: "muted" }, v.note || "—"),
          h("td", { class: "num" }, String(v.sessions)),
          h("td", {}, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            iconButton("download", `Descargar r${v.revision}`, () => { location.href = `/api/bo/profiles/${id}/versions/${v.revision}`; }),
            button("Restaurar al borrador", { size: "sm", icon: "undo", onClick: async () => {
              if (timer) await saveNow();
              await api.post(`/api/bo/profiles/${id}/versions/${v.revision}/restore`);
              toast(`Versión ${v.revision} copiada al borrador`);
              reload();
            } })))))))),
    ];
  }

  // ---------- simulator ----------

  function openSimulator() {
    const sim = { messages: [], source: detail.has_draft || base ? "draft" : "published" };
    const transcript = h("div", { class: "sim-transcript", testid: "sim-output" });
    const stateBox = h("dl", { class: "sim-state" });
    const input = h("input", { placeholder: "Escribe como si fueras el candidato…", "aria-label": "Mensaje de prueba" });
    async function run() {
      if (timer) await saveNow();
      let r;
      try {
        r = await api.post(`/api/bo/profiles/${id}/simulate`, { messages: sim.messages, source: sim.source });
      } catch (err) {
        clear(transcript, h("div", { class: "callout bad" }, err.message));
        return;
      }
      clear(transcript, r.transcript.map((m) => h("div", { class: `msg ${m.role === "user" ? "cand" : m.role === "system" ? "sys" : "bot"}` },
        h("div", { class: "msg-body" }, m.content))));
      transcript.scrollTop = transcript.scrollHeight;
      const statusTone = { qualified: "ok", disqualified: "bad", abandoned: "warn" }[r.status] || "info";
      clear(stateBox,
        h("dt", {}, "Estado"), h("dd", {}, chip(r.status, statusTone, { testid: "sim-status" }), r.disqualify_reason ? chip(r.disqualify_reason, "bad") : null),
        h("dt", {}, "Puntuación"), h("dd", {}, scoreBadge(r.score)),
        h("dt", {}, "Alertas"), h("dd", {}, r.flags.length ? r.flags.map((f) => chip(f, "chip-flag")) : h("span", { class: "muted" }, "—")),
        r.pending_question ? [h("dt", {}, "Siguiente"), h("dd", { class: "small" }, r.pending_question)] : null);
    }
    const send = () => { if (input.value.trim()) { sim.messages.push(input.value.trim()); input.value = ""; run(); } };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    drawer("Probar el proceso", h("div", { class: "sim" },
      h("div", { class: "row between" },
        segmented([["draft", "Borrador"], ["published", "Publicado"]], sim.source, (v) => { sim.source = v; run(); }, "Versión a probar"),
        button("Reiniciar", { icon: "refresh", variant: "ghost", size: "sm", onClick: () => { sim.messages = []; run(); } })),
      h("p", { class: "hint" }, "Motor de reglas sin modelo de lenguaje; no se guarda nada. Documentos de prueba: 12345678Z válido · 99000001C no consta · 98000001Z error."),
      transcript,
      h("div", { class: "sim-input" }, input, button("", { icon: "send", variant: "primary", "aria-label": "Enviar mensaje de prueba", onClick: send })),
      stateBox), { width: 560, subtitle: loc(doc.role, "es") });
    setTimeout(() => input.focus(), 50);
    run();
  }

  // ---------- render ----------

  const RENDER = { general, questions, rules, scoring, checks, messages, faq, areas, history: versions };

  function renderPanel() {
    clear(panel, RENDER[section]());
    markProblems();
  }

  function renderAll() {
    renderHead();
    renderProblems();
    renderNav();
    renderPanel();
  }

  clear(root, head, problemsEl, h("div", { class: "editor-layout" }, nav, h("div", {}, panel)));
  renderAll();
  return () => { if (timer) saveNow(); };
}
