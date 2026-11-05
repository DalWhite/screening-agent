// Shared backoffice state (metadata, global process filter) and domain widgets
// reused by every view, so the same thing always looks and behaves the same.

import { api, qs } from "./api.js";
import { button, chip, dot, h, icon, loc, menu, modal, profileTone, toast } from "./kit.js";

export const store = {
  meta: null,
  profile: localStorage.getItem("bo_profile") || "",
  listeners: new Set(),
};

export async function loadMeta(force = false) {
  if (!store.meta || force) store.meta = await api.get("/api/bo/meta");
  return store.meta;
}

export function setProfile(profileId) {
  store.profile = profileId || "";
  localStorage.setItem("bo_profile", store.profile);
  for (const fn of store.listeners) fn(store.profile);
}

export function onProfileChange(fn) {
  store.listeners.add(fn);
  return () => store.listeners.delete(fn);
}

// ---------- routing ----------

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  return { parts: (path || "pipeline").split("/").filter(Boolean), params: new URLSearchParams(query || "") };
}

export function navigate(path, params) {
  const query = params ? qs(params) : "";
  location.hash = `#/${path}${query ? "?" + query : ""}`;
}

export function candidateHref(id, tab) {
  return `#/candidates/${id}${tab ? "?tab=" + tab : ""}`;
}

// ---------- labels ----------

export function pipelineStages() {
  return store.meta?.pipeline || [];
}

export function stageById(id) {
  return pipelineStages().find((s) => s.id === id);
}

export function pipelineLabel(id) {
  const stage = stageById(id);
  return stage ? loc(stage.label) : id;
}

export function stageTone(id) {
  return stageById(id)?.color || "gray";
}

export function statusLabel(status) {
  return loc(store.meta?.statuses?.[status]) || status;
}

export function sourceLabel(source) {
  return loc(store.meta?.sources?.[source]) || source;
}

export function flagLabel(flag) {
  const labels = store.meta?.flags || {};
  if (labels[flag]) return labels[flag];
  const [base, detail] = flag.split(":");
  if (!labels[base]) return flag;
  if (!detail) return labels[base];
  let detailLabel = detail;
  for (const p of profiles()) {
    const check = (p.checks || []).find((c) => c.id === detail);
    const question = p.questions.find((q) => q.id === detail);
    if (check) { detailLabel = loc(check.label); break; }
    if (question) { detailLabel = loc(question.label); break; }
  }
  return `${labels[base]}: ${detailLabel}`;
}

const TONES = ["indigo", "amber", "teal", "pink", "violet", "blue", "orange", "green"];

/** Stable, distinct colour per process (by position in the process list). */
export function processTone(profileId) {
  const i = profiles().findIndex((p) => p.id === profileId);
  return i >= 0 ? TONES[i % TONES.length] : profileTone(profileId);
}

export function profiles() {
  return store.meta?.profiles || [];
}

export function profileById(id) {
  return profiles().find((p) => p.id === id);
}

const LIFECYCLE_STAGES = { intro: "saludo", review: "cierre", verifying: "verificando datos" };

export function questionLabel(profileId, questionId) {
  const q = profileById(profileId)?.questions.find((x) => x.id === questionId);
  return q ? loc(q.label) : LIFECYCLE_STAGES[questionId] || questionId;
}

export function faqLabel(faqId) {
  for (const p of profiles()) {
    const text = p.faq_topics?.[faqId];
    if (text) return text;
  }
  return faqId;
}

export function profileName(id) {
  const p = profileById(id);
  return p ? loc(p.role) : id;
}

// ---------- widgets ----------

const STATUS_TONE = { active: "blue", verifying: "violet", qualified: "green", disqualified: "red", abandoned: "amber" };

export function statusChip(status) {
  return chip([dot(STATUS_TONE[status] || "gray"), statusLabel(status)], "chip-status", { testid: "status-chip" });
}

export function pipelineChip(stage) {
  return chip([dot(stageTone(stage)), pipelineLabel(stage)], `chip-stage tone-${stageTone(stage)}`, { testid: "pipeline-chip" });
}

export function profileChip(profileId, short = false) {
  const p = profileById(profileId);
  const text = p ? (short ? p.company : `${loc(p.role)} · ${p.company}`) : profileId;
  return chip([dot(processTone(profileId)), text], "chip-profile", { title: p ? `${loc(p.role)} — ${p.company}` : profileId });
}

const SEVERE = new Set(["self_harm", "abusive_language", "physical_review", "requested_human_callback", "needs_review", "check_error"]);

export function flagChips(flags, max = 3) {
  const list = flags || [];
  const shown = list.slice(0, max).map((f) => chip(flagLabel(f), SEVERE.has(f.split(":")[0]) ? "chip-flag severe" : "chip-flag", { title: f }));
  if (list.length > max) shown.push(chip(`+${list.length - max}`, "chip-more", { title: list.slice(max).map(flagLabel).join(", ") }));
  return shown;
}

export async function moveCandidate(id, stage) {
  await api.patch(`/api/bo/candidates/${id}`, { pipeline_stage: stage });
  toast(`Movido a «${pipelineLabel(stage)}»`);
}

/** Stage as a coloured pill; clicking it opens the stage menu (move in two clicks from anywhere). */
export function stagePill(candidate, onMoved, { label } = {}) {
  const current = candidate.pipeline_stage;
  const pill = h("button", { type: "button", class: `stage-pill tone-${stageTone(current)}`, testid: "stage-pill",
                             "aria-label": label || `Etapa de ${candidate.name || candidate.display_name || "candidato"}: ${pipelineLabel(current)}`,
                             "aria-haspopup": "menu" },
    dot(stageTone(current)), h("span", {}, pipelineLabel(current)), icon("chevronDown", 13));
  pill.addEventListener("click", (e) => {
    e.stopPropagation();
    menu(pill, [{ header: "Mover a" }, ...pipelineStages().map((s) => ({
      label: loc(s.label), tone: s.color, checked: s.id === current, testid: `move-${s.id}`,
      onClick: async () => {
        if (s.id === current) return;
        try { await moveCandidate(candidate.id, s.id); onMoved?.(s.id); } catch (err) { toast(err.message, "error"); }
      },
    }))]);
  });
  return pill;
}

/** The stage after the current one (for the one-click "advance" action), skipping "rejected". */
export function nextStage(current) {
  const stages = pipelineStages().filter((s) => s.id !== "rejected");
  const i = stages.findIndex((s) => s.id === current);
  return i >= 0 && i < stages.length - 1 ? stages[i + 1] : null;
}

// ---------- new candidate ----------

export function openNewCandidate() {
  const active = profiles().filter((p) => p.active);
  const form = h("form", { class: "form-grid", id: "new-candidate-form" },
    h("label", { class: "field span-2" }, h("span", {}, "Proceso de selección"),
      h("select", { name: "profile_id", required: true },
        active.map((p) => h("option", { value: p.id, selected: p.id === store.profile }, `${loc(p.role)} — ${p.company}`)))),
    h("label", { class: "field" }, h("span", {}, "Nombre y apellidos"), h("input", { name: "name", autocomplete: "off", placeholder: "Jorge Blanco Ruiz" })),
    h("label", { class: "field" }, h("span", {}, "Contacto (teléfono o email)"), h("input", { name: "contact", autocomplete: "off", placeholder: "+34 600 000 000" })),
    h("label", { class: "field" }, h("span", {}, "Idioma"),
      h("select", { name: "language" }, h("option", { value: "es" }, "Español"), h("option", { value: "en" }, "English"))),
    h("label", { class: "field span-2" }, h("span", {}, "Nota interna (opcional)"), h("textarea", { name: "note", rows: 2, placeholder: "Origen, referido por…" })),
  );
  const submit = button("Crear candidato", { variant: "primary", type: "submit", form: "new-candidate-form" });
  const { close, dialog } = modal("Nuevo candidato", form, [submit],
    { description: "Regístralo y envíale su enlace: el asistente hará el screening con él." });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    submit.disabled = true;
    try {
      const created = await api.post("/api/bo/candidates", data);
      const link = `${location.origin}${created.chat_link}`;
      dialog.querySelector(".modal-body").replaceChildren(
        h("div", { class: "callout ok" }, icon("check", 16), h("span", {}, "Candidato creado. Envíale este enlace para que haga el screening:")),
        h("div", { class: "copy-row" },
          h("input", { value: link, readonly: true, "aria-label": "Enlace de screening", testid: "screening-link" }),
          button("Copiar", { icon: "copy", onClick: () => navigator.clipboard?.writeText(link).then(() => toast("Enlace copiado")) })),
      );
      dialog.querySelector("footer").replaceChildren(
        h("a", { class: "btn btn-primary", href: candidateHref(created.id), onclick: close }, h("span", {}, "Ver ficha del candidato")));
    } catch (err) {
      toast(err.message, "error");
      submit.disabled = false;
    }
  });
}
