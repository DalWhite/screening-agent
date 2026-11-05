// Candidate quick view: a side panel that lets a recruiter triage without
// leaving the list or board (move stage, advance, reject, read the key answers,
// reply and take a note). Views refresh on the "bo:candidate-changed" event.

import { api } from "../api.js";
import {
  avatar, button, chip, clear, drawer, fmtDateTime, h, icon, linkButton, relTime, scoreBadge, spinner, toast,
} from "../kit.js";
import {
  candidateHref, flagChips, moveCandidate, nextStage, pipelineLabel, profileChip, stagePill, statusChip,
} from "../shared.js";

const CHECK_TONE = { valid: "ok", invalid: "bad", error: "warn", pending: "info", not_requested: "muted" };

export function notifyChanged(id) {
  window.dispatchEvent(new CustomEvent("bo:candidate-changed", { detail: { id } }));
}

export async function openQuickView(id) {
  const { body, close, panel } = drawer("Candidato", spinner(), { width: 540 });
  panel.setAttribute("data-testid", "quick-view");

  async function render() {
    let c;
    try {
      c = await api.get(`/api/bo/candidates/${id}`);
    } catch (err) {
      clear(body, h("div", { class: "callout bad" }, err.message));
      return;
    }
    const name = c.display_name || "Sin nombre";
    panel.querySelector(".drawer-title h2").textContent = name;
    const changed = async () => { notifyChanged(id); await render(); };
    const next = nextStage(c.pipeline_stage);
    const answers = c.report.answers.filter((a) => a.display && a.display !== "—");
    const checks = c.report.checks || [];
    const lastMessages = c.messages.slice(-4);

    const reply = h("input", { placeholder: "Responder al candidato…", "aria-label": "Respuesta rápida", maxlength: 1000 });
    const sendReply = async () => {
      if (!reply.value.trim()) return;
      await api.post(`/api/bo/candidates/${id}/messages`, { text: reply.value });
      toast("Mensaje enviado · asistente en pausa");
      await changed();
    };
    reply.addEventListener("keydown", (e) => { if (e.key === "Enter") sendReply(); });
    const note = h("input", { placeholder: "Añadir una nota interna…", "aria-label": "Nota rápida" });
    const saveNote = async () => {
      if (!note.value.trim()) return;
      await api.post(`/api/bo/candidates/${id}/notes`, { body: note.value });
      toast("Nota añadida");
      await changed();
    };
    note.addEventListener("keydown", (e) => { if (e.key === "Enter") saveNote(); });

    clear(body,
      h("div", { class: "qv-head" }, avatar(name, "lg"),
        h("div", { class: "grow" },
          h("div", { class: "row" }, profileChip(c.profile_id), statusChip(c.status)),
          h("div", { class: "muted small", style: { marginTop: "4px" } },
            [c.display_city, c.contact, `activo ${relTime(c.last_ts)}`].filter(Boolean).join(" · "))),
        scoreBadge(c.score, true)),
      h("div", { class: "qv-actions" },
        stagePill({ ...c, name }, changed),
        next && c.pipeline_stage !== "rejected" ? button(`Avanzar a ${pipelineLabel(next.id)}`, {
          icon: "arrowRight", variant: "primary", size: "sm", testid: "advance",
          onClick: async () => { await moveCandidate(id, next.id); await changed(); } }) : null,
        c.pipeline_stage !== "rejected" ? button("Descartar", { icon: "ban", variant: "danger", size: "sm",
          onClick: async () => { await moveCandidate(id, "rejected"); await changed(); } }) : null,
        linkButton("Abrir ficha", candidateHref(id), { icon: "external", variant: "ghost", size: "sm", onclick: close })),
      c.bot_paused ? h("div", { class: "callout info" }, icon("pause", 16), h("span", {}, "Estás atendiendo tú esta conversación: el asistente está en pausa.")) : null,
      c.report.flags.length ? h("section", { class: "qv-section" }, h("h3", {}, "Alertas"), h("div", { class: "flag-list" }, flagChips(c.flags, 6))) : null,
      h("section", { class: "qv-section" }, h("h3", {}, "Respuestas"),
        answers.length ? h("dl", { class: "qv-data" }, answers.map((a) => [h("dt", {}, a.label), h("dd", {}, a.display)]))
          : h("p", { class: "muted small" }, "Todavía no ha respondido ninguna pregunta.")),
      checks.length ? h("section", { class: "qv-section" }, h("h3", {}, "Verificaciones"),
        h("div", { class: "row" }, checks.map((k) => chip(`${k.label}: ${k.status_label}`, CHECK_TONE[k.status] || "muted")))) : null,
      h("section", { class: "qv-section" }, h("h3", {}, "Conversación"),
        h("div", { class: "qv-messages" }, lastMessages.map((m) => {
          const cls = m.role === "user" ? "cand" : m.role === "recruiter" ? "recruiter" : "bot";
          return h("div", { class: `msg ${cls}` }, h("div", { class: "msg-meta" }, fmtDateTime(m.ts)), h("div", { class: "msg-body" }, m.content));
        })),
        h("div", { class: "qv-reply", style: { marginTop: "10px" } }, reply, button("", { icon: "send", variant: "primary", "aria-label": "Enviar respuesta", onClick: sendReply }))),
      h("section", { class: "qv-section" }, h("h3", {}, "Notas"),
        h("div", { class: "qv-reply" }, note, button("Añadir", { onClick: saveNote })),
        (c.notes_recruiter || []).slice(0, 3).map((n) => h("p", { class: "small", style: { marginTop: "8px" } },
          h("strong", {}, n.author), " · ", h("span", { class: "muted" }, relTime(n.ts)), h("br"), n.body))));
  }

  await render();
}
