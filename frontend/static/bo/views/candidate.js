// Candidate file: profile (editable data, owner, tags, notes), screening results
// (answers, external checks, rules, score), conversation with human takeover,
// and the activity timeline.

import { api } from "../api.js";
import {
  avatar, button, chip, clear, emptyState, fmtDateTime, fmtDuration, h, icon, iconButton, loc, menu, modal, relTime,
  scoreBadge, toast,
} from "../kit.js";
import {
  flagLabel, loadMeta, moveCandidate, nextStage, pipelineLabel, profileChip, sourceLabel, stagePill, statusChip, store,
} from "../shared.js";

const TABS = [
  { id: "summary", label: "Perfil", icon: "user" },
  { id: "screening", label: "Resultados del screening", icon: "rules" },
  { id: "conversation", label: "Conversación", icon: "message" },
  { id: "activity", label: "Actividad", icon: "history" },
];

const ANSWER_STATUS = {
  answered: ["Respondida", "ok"], knockout: ["Eliminatoria", "bad"], unresolved: ["Sin validar", "warn"],
  pending: ["Pendiente", "info"], missing: ["Sin respuesta", "muted"],
};
const RULE_RESULT = {
  passed: ["Cumple", "ok"], triggered: ["Activada", "bad"], not_evaluated: ["No evaluada", "muted"], pending: ["En espera", "info"],
};
const CHECK_KIND = { valid: "ok", invalid: "bad", error: "warn", pending: "info", not_requested: "muted" };
const TECHNICAL_EVENTS = new Set(["validation_fail", "location_match", "field_fallback", "code_switch", "language_switch",
  "rag_hit", "rag_miss", "phrasing_fallback", "llm_error", "stage_advance", "ignored_after_close", "give_up_field",
  "channel_session_started"]);

export async function renderCandidate(root, id, params) {
  await loadMeta();
  let tab = TABS.some((t) => t.id === params.get("tab")) ? params.get("tab") : "summary";
  let c = await api.get(`/api/bo/candidates/${id}`);
  let poll = null;

  const header = h("div", { class: "cand-head" });
  const tabsEl = h("div", { class: "tabs", role: "tablist", "aria-label": "Secciones del candidato" });
  const panel = h("section", { class: "tab-panel", role: "tabpanel" });

  async function reload() {
    c = await api.get(`/api/bo/candidates/${id}`);
    renderHeader();
    renderTabs();
    renderTab();
  }

  // ---------- header ----------

  function renderHeader() {
    const name = c.display_name || "Candidato sin nombre";
    const next = nextStage(c.pipeline_stage);
    const copyLink = () => {
      const link = `${location.origin}${c.chat_link}`;
      navigator.clipboard?.writeText(link).then(() => toast("Enlace de chat copiado"), () => toast(link));
    };
    const more = iconButton("more", "Más acciones", () => menu(more, [
      { label: "Copiar enlace de chat del candidato", icon: "link", onClick: copyLink },
      { label: "Escribir al candidato", icon: "message", onClick: () => switchTab("conversation") },
      { label: "Ver actividad", icon: "history", onClick: () => switchTab("activity") },
    ], { align: "end" }));
    clear(header,
      h("a", { class: "back", href: "#/candidates" }, icon("arrowLeft", 14), "Candidatos"),
      h("div", { class: "cand-title" },
        avatar(name, "lg"),
        h("div", { class: "grow" },
          h("h1", { testid: "candidate-name" }, name),
          h("div", { class: "cand-sub" },
            profileChip(c.profile_id), statusChip(c.status),
            c.display_city ? h("span", { class: "meta" }, icon("pin", 14), c.display_city) : null,
            c.contact ? h("span", { class: "meta" }, icon("phone", 14), c.contact) : null,
            h("span", { class: "meta" }, icon("message", 14), sourceLabel(c.source)),
            c.bot_paused ? chip("Atendido por reclutador", "info") : null)),
        h("div", { class: "cand-score" }, scoreBadge(c.score, true), h("span", { class: "muted small" }, "puntuación"))),
      h("div", { class: "cand-actions" },
        stagePill({ ...c, name }, () => reload()),
        next && c.pipeline_stage !== "rejected" ? button(`Avanzar a ${pipelineLabel(next.id)}`, { icon: "arrowRight", variant: "primary",
          testid: "advance", onClick: async () => { await moveCandidate(id, next.id); reload(); } }) : null,
        button("Programar entrevista", { icon: "calendar", onClick: scheduleInterview }),
        c.pipeline_stage !== "rejected" ? button("Descartar", { icon: "ban", variant: "danger", onClick: async () => { await moveCandidate(id, "rejected"); reload(); } }) : null,
        c.interview_at ? chip([icon("calendar", 13), `Entrevista: ${fmtDateTime(c.interview_at)}`], "info", { testid: "interview-chip" }) : null,
        h("div", { class: "grow" }), more));
  }

  function scheduleInterview() {
    const input = h("input", { type: "datetime-local", value: (c.interview_at || "").slice(0, 16), "aria-label": "Fecha y hora de la entrevista" });
    const hasInterview = store.meta?.pipeline?.some((s) => s.id === "interview");
    const save = button(hasInterview ? "Guardar y mover a Entrevista" : "Guardar", { variant: "primary", onClick: async () => {
      if (!input.value) return toast("Elige fecha y hora", "error");
      await api.patch(`/api/bo/candidates/${id}`, { interview_at: input.value, ...(hasInterview ? { pipeline_stage: "interview" } : {}) });
      toast("Entrevista programada");
      close();
      reload();
    } });
    const { close } = modal("Programar entrevista", h("label", { class: "field" }, h("span", {}, "Fecha y hora"), input), [save],
      { description: hasInterview ? "El candidato pasará a la etapa «Entrevista»." : "" });
  }

  // ---------- tabs ----------

  function switchTab(t) {
    tab = t;
    history.replaceState(null, "", `#/candidates/${id}?tab=${tab}`);
    renderTabs();
    renderTab();
  }

  function renderTabs() {
    clear(tabsEl, TABS.map((t) => h("button", {
      role: "tab", class: "tab", "aria-selected": String(t.id === tab), testid: `tab-${t.id}`, onclick: () => switchTab(t.id),
    }, icon(t.icon, 15), t.label, t.id === "conversation" && c.bot_paused ? h("span", { class: "dot", title: "Atendida por reclutador" }) : null)));
  }

  function renderTab() {
    clearInterval(poll);
    poll = null;
    if (tab === "summary") renderSummary();
    else if (tab === "screening") renderScreening();
    else if (tab === "conversation") renderConversation();
    else renderActivity();
  }

  // ---------- profile tab ----------

  function renderSummary() {
    const answers = Object.fromEntries(c.report.answers.map((a) => [a.id, a]));
    const assignee = h("input", { value: c.assignee || "", placeholder: "Sin asignar", list: "assignees", "aria-label": "Responsable" });
    assignee.addEventListener("change", async () => {
      await api.patch(`/api/bo/candidates/${id}`, { assignee: assignee.value });
      await loadMeta(true);
      await reload();
      toast("Responsable actualizado");
    });
    const tagInput = h("input", { placeholder: "Añadir etiqueta…", "aria-label": "Nueva etiqueta", list: "all-tags" });
    tagInput.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !tagInput.value.trim()) return;
      e.preventDefault();
      await api.patch(`/api/bo/candidates/${id}`, { tags: [...(c.tags || []), tagInput.value.trim()] });
      await loadMeta(true);
      reload();
    });
    const noteBox = h("textarea", { rows: 3, placeholder: "Escribe una nota interna (no la ve el candidato)…", "aria-label": "Nueva nota" });
    const addNote = button("Añadir nota", { variant: "primary", size: "sm", onClick: async () => {
      if (!noteBox.value.trim()) return;
      await api.post(`/api/bo/candidates/${id}/notes`, { body: noteBox.value });
      await reload();
      toast("Nota añadida");
    } });
    const meta = store.meta || {};

    clear(panel,
      h("div", { class: "grid-2" },
        h("div", { class: "stack" },
          h("section", { class: "panel-card" },
            h("div", { class: "section-title" }, h("div", {}, h("h3", {}, "Datos del candidato"),
              h("p", { class: "muted small" }, "Recogidos por el asistente. Pasa el ratón por un dato para corregirlo."))),
            h("dl", { class: "data-list", testid: "data-list" }, c.profile.questions.map((q) => dataRow(q, answers[q.id])))),
          h("section", { class: "panel-card" },
            h("div", { class: "section-title" }, h("h3", {}, "Información")),
            h("dl", { class: "info-list" },
              info("Canal", sourceLabel(c.source)), info("Contacto", c.contact || "—"),
              info("Idiomas", (c.languages || [c.language]).join(", ") || c.language),
              info("Inicio", fmtDateTime(c.started_at)), info("Última actividad", fmtDateTime(c.last_ts)),
              info("Duración", fmtDuration(c.duration_seconds)), info("Turnos", String(c.candidate_turns ?? 0)),
              info("Versión del proceso", `r${c.profile.revision}`), info("ID", c.id)))),
        h("div", { class: "stack" },
          h("section", { class: "panel-card" },
            h("div", { class: "section-title" }, h("h3", {}, "Gestión")),
            h("div", { class: "stack" },
              h("label", { class: "field" }, h("span", {}, "Responsable"), assignee,
                h("datalist", { id: "assignees" }, (meta.assignees || []).map((a) => h("option", { value: a })))),
              h("div", { class: "field" }, h("span", {}, "Etiquetas"),
                h("div", { class: "tags", testid: "tags" },
                  (c.tags || []).map((t) => chip([`#${t}`, h("button", { type: "button", "aria-label": `Quitar etiqueta ${t}`, onclick: async () => {
                    await api.patch(`/api/bo/candidates/${id}`, { tags: c.tags.filter((x) => x !== t) });
                    reload();
                  } }, icon("x", 12))], "chip-tag")),
                  tagInput, h("datalist", { id: "all-tags" }, (meta.tags || []).map((t) => h("option", { value: t }))))),
              h("div", { class: "field" }, h("span", {}, "Entrevista"),
                h("div", { class: "row" }, c.interview_at ? fmtDateTime(c.interview_at) : h("span", { class: "muted" }, "Sin programar"),
                  button(c.interview_at ? "Cambiar" : "Programar", { size: "sm", variant: "ghost", icon: "calendar", onClick: scheduleInterview }))))),
          h("section", { class: "panel-card" },
            h("div", { class: "section-title" }, h("h3", {}, "Notas internas")),
            noteBox, h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "8px" } }, addNote),
            h("ul", { class: "notes", testid: "notes" },
              (c.notes_recruiter || []).map((n) => h("li", {},
                h("div", { class: "note-head" }, avatar(n.author), h("strong", {}, n.author), h("span", { class: "muted", title: n.ts }, relTime(n.ts)),
                  iconButton("trash", "Borrar nota", async () => { await api.del(`/api/bo/candidates/${id}/notes/${n.id}`); reload(); })),
                h("p", {}, n.body))),
              !(c.notes_recruiter || []).length ? h("li", { class: "muted small" }, "Todavía no hay notas.") : null)))));
  }

  function info(label, value) {
    return [h("dt", {}, label), h("dd", {}, value)];
  }

  function dataRow(q, answer) {
    const value = h("dd", { testid: `value-${q.id}` }, answer?.display || "—",
      answer && answer.status !== "answered" ? [" ", chip(ANSWER_STATUS[answer.status][0], ANSWER_STATUS[answer.status][1])] : null);
    return [h("dt", {}, loc(q.label)), h("div", { class: "dd-row" }, value,
      iconButton("pencil", `Editar ${loc(q.label)}`, () => openEditor(q)))];
  }

  function editorFor(q, key, current) {
    if (q.type === "yes_no") {
      return h("select", { name: key, "aria-label": loc(q.label) }, [["", "—"], ["true", "Sí"], ["false", "No"]]
        .map(([v, l]) => h("option", { value: v, selected: String(current) === v }, l)));
    }
    if (q.type === "choice" && key === q.field) {
      return h("select", { name: key, "aria-label": loc(q.label) }, h("option", { value: "" }, "—"),
        q.options.map((o) => h("option", { value: o.value, selected: current === o.value }, loc(o.label))));
    }
    if (q.type === "multi_choice") {
      return h("div", { class: "stack", style: { gap: "6px" }, dataset: { name: key } }, q.options.map((o) =>
        h("label", { class: "check" }, h("input", { type: "checkbox", value: o.value, checked: (current || []).includes(o.value) }), loc(o.label))));
    }
    if ((q.type === "number" || q.type === "experience") && key === q.field) return h("input", { type: "number", name: key, value: current ?? "", min: 0 });
    if (q.type === "date") return h("input", { type: "date", name: key, value: current || "" });
    if (q.type === "id_document") return h("input", { name: key, value: "", placeholder: current ? `Actual: ${current}` : "12345678Z", autocomplete: "off" });
    return h("input", { name: key, value: Array.isArray(current) ? current.join(", ") : current ?? "" });
  }

  function openEditor(q) {
    const keys = q.type === "location" ? [q.field, "zone"] : q.type === "experience" && q.tags_field ? [q.field, q.tags_field] : [q.field];
    const labels = { zone: "Zona / barrio", [q.tags_field]: "Detalle (separado por comas)" };
    const editors = keys.map((key) => [key, editorFor(q, key, c.data[key])]);
    const form = h("div", { class: "stack" }, editors.map(([key, el]) => el.dataset.name
      ? h("fieldset", { class: "checks-field" }, h("legend", {}, labels[key] || loc(q.label)), el)
      : h("label", { class: "field" }, h("span", {}, labels[key] || loc(q.label)), el)));
    const save = button("Guardar", { variant: "primary", onClick: async () => {
      const changes = {};
      for (const [key, el] of editors) {
        if (el.dataset.name) changes[key] = [...el.querySelectorAll("input:checked")].map((i) => i.value);
        else if (q.type === "yes_no") changes[key] = el.value === "" ? null : el.value === "true";
        else changes[key] = el.value;
      }
      try {
        await api.patch(`/api/bo/candidates/${id}/data`, { changes });
        toast("Dato actualizado");
        close();
        reload();
      } catch (err) { toast(err.message, "error"); }
    } });
    const { close } = modal(`Editar: ${loc(q.label)}`, form, [save], { description: "La corrección queda registrada en la actividad del candidato." });
  }

  // ---------- screening results tab ----------

  function renderScreening() {
    const r = c.report;
    const o = r.outcome;
    const outcome = (kind, iconName, title, detail) => h("div", { class: `banner ${kind}`, testid: "outcome-banner" },
      h("span", { class: "row" }, icon(iconName, 16), h("span", {}, h("strong", {}, title), detail ? ` · ${detail}` : "")));
    const banner = o.status === "qualified"
      ? outcome(c.pipeline_stage === "review" ? "warn" : "ok", "check", "Pre-calificado por el asistente",
          c.pipeline_stage === "review" ? "cumple los requisitos, pero tiene alertas que conviene revisar." : "cumple todos los requisitos obligatorios.")
      : o.status === "disqualified" ? outcome("bad", "ban", "No califica", o.disqualify_label || o.disqualify_reason || "")
        : o.status === "abandoned" ? outcome("warn", "alert", "Screening incompleto", o.close_reason ? `motivo: ${flagLabel(o.close_reason)}` : "")
          : o.status === "verifying" ? outcome("info", "shield", "En verificación", "ha respondido todo; la decisión espera a las verificaciones externas.")
            : outcome("info", "message", "Screening en curso", c.pending_question ? `pregunta pendiente: «${c.pending_question}»` : "");
    const pct = r.completion.required ? Math.round((100 * r.completion.answered) / r.completion.required) : 0;
    clear(panel,
      banner,
      h("div", { class: "grid-3" },
        h("section", { class: "panel-card" }, h("h3", {}, "Completado"),
          h("div", { class: "big-num" }, `${r.completion.answered}/${r.completion.required}`),
          h("div", { class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 }, h("span", { style: { width: pct + "%" } })),
          h("p", { class: "muted small" }, "preguntas obligatorias validadas")),
        h("section", { class: "panel-card" }, h("div", { class: "row between" }, h("h3", {}, "Puntuación"), scoreBadge(r.score)),
          h("ul", { class: "breakdown", testid: "score-breakdown" }, r.score_breakdown.map((b) => h("li", {},
            h("span", {}, b.label), h("span", { class: "bar" }, h("span", { style: { width: (b.max ? (100 * b.points) / b.max : 0) + "%" } })),
            h("span", { class: "muted small", style: { textAlign: "right" } }, `${b.points}/${b.max}`))))),
        h("section", { class: "panel-card" }, h("h3", {}, "Alertas"),
          r.flags.length ? h("div", { class: "flag-list", style: { marginTop: "10px" } }, r.flags.map((f) => chip(f.label, "chip-flag", { title: f.id })))
            : h("p", { class: "muted small", style: { marginTop: "10px" } }, "Sin alertas."),
          r.agent_notes.length ? h("ul", { class: "agent-notes" }, r.agent_notes.map((n) => h("li", {}, n))) : null,
          r.recruiter_summary ? h("p", { class: "summary-line" }, r.recruiter_summary) : null)),
      h("section", { class: "panel-card" },
        h("div", { class: "section-title" }, h("h3", {}, "Respuestas por pregunta")),
        h("div", { class: "table-wrap" }, h("table", { class: "table", testid: "answers-table" },
          h("thead", {}, h("tr", {}, ["Pregunta", "Respuesta", "Estado", "Intentos", "Lo que dijo el candidato"].map((t) => h("th", { class: t === "Intentos" ? "num" : "" }, t)))),
          h("tbody", {}, r.answers.map((a) => h("tr", { dataset: { question: a.id } },
            h("td", {}, h("strong", {}, a.label), a.required ? null : h("span", { class: "muted small" }, " (opcional)"),
              h("div", { class: "muted small" }, a.question)),
            h("td", {}, a.display),
            h("td", {}, chip(ANSWER_STATUS[a.status][0], ANSWER_STATUS[a.status][1])),
            h("td", { class: "num" }, String(a.attempts || 0)),
            h("td", {}, a.raw_answers.length ? a.raw_answers.map((t) => h("q", { class: "raw" }, t)) : h("span", { class: "muted" }, "—")))))))),
      r.checks.length ? checksCard(r.checks) : null,
      h("section", { class: "panel-card" },
        h("div", { class: "section-title" }, h("h3", {}, "Reglas del proceso")),
        h("div", { class: "table-wrap" }, h("table", { class: "table", testid: "rules-table" },
          h("thead", {}, h("tr", {}, ["Regla", "Tipo", "Condición", "Resultado"].map((t) => h("th", {}, t)))),
          h("tbody", {}, r.rules.map((rule) => h("tr", { dataset: { rule: rule.id } },
            h("td", {}, rule.label),
            h("td", {}, chip(rule.action === "disqualify" ? "Eliminatoria" : "Alerta", rule.action === "disqualify" ? "bad" : "warn")),
            h("td", { class: "muted" }, rule.condition),
            h("td", {}, chip(RULE_RESULT[rule.result][0], RULE_RESULT[rule.result][1])))))))));
  }

  function checksCard(checks) {
    if (checks.some((k) => k.status === "pending")) {
      poll = setInterval(async () => {
        if (document.hidden) return;
        const fresh = await api.get(`/api/bo/candidates/${id}`).catch(() => null);
        if (fresh && JSON.stringify(fresh.report.checks.map((k) => k.status)) !== JSON.stringify(checks.map((k) => k.status))) {
          clearInterval(poll);
          c = fresh;
          renderHeader();
          renderTab();
        }
      }, 3000);
    }
    const decide = (check, status) => async () => {
      try {
        await api.post(`/api/bo/candidates/${id}/verifications/${check.verification.id}/decision`,
          { status, note: status === "valid" ? "Verificado por el reclutador" : "Rechazado por el reclutador" });
        await reload();
        toast(`Verificación marcada como ${status === "valid" ? "válida" : "no válida"}`);
      } catch (err) { toast(err.message, "error"); }
    };
    return h("section", { class: "panel-card" },
      h("div", { class: "section-title" }, h("div", {}, h("h3", {}, "Verificaciones externas"),
        h("p", { class: "muted small" }, "Las responde un sistema externo de forma asíncrona (o tú, si son manuales). Al llegar el resultado se vuelven a aplicar las reglas."))),
      h("div", { class: "table-wrap" }, h("table", { class: "table", testid: "checks-table" },
        h("thead", {}, h("tr", {}, ["Verificación", "Proveedor", "Estado", "Detalle", "Fechas", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, checks.map((k) => {
          const v = k.verification;
          return h("tr", { dataset: { check: k.id } },
            h("td", {}, h("strong", {}, k.label), h("div", { class: "muted small" }, `${k.blocking ? "Bloqueante" : "No bloqueante"} · se lanza con: ${k.trigger}`)),
            h("td", { class: "small" }, k.provider_label),
            h("td", {}, chip(k.status_label, CHECK_KIND[k.status] || "muted", { testid: "check-status" })),
            h("td", { class: "small" }, v?.result_detail || "—", v?.decided_by ? h("div", { class: "muted" }, `por ${v.decided_by}`) : null),
            h("td", { class: "small muted" }, v ? `Solicitada ${fmtDateTime(v.created_at)}` : "—", v?.completed_at ? h("div", {}, `Respondida ${fmtDateTime(v.completed_at)}`) : null),
            h("td", {}, h("div", { class: "row", style: { justifyContent: "flex-end" } },
              k.can_decide ? [
                button("Válido", { icon: "check", size: "sm", onClick: decide(k, "valid"), "aria-label": `Marcar ${k.label} como válida` }),
                button("No válido", { icon: "x", size: "sm", variant: "danger", onClick: decide(k, "invalid"), "aria-label": `Marcar ${k.label} como no válida` }),
              ] : null,
              k.can_retry ? button("Reintentar", { icon: "refresh", size: "sm", onClick: async () => {
                await api.post(`/api/bo/candidates/${id}/verifications/${v.id}/retry`);
                await reload();
                toast("Verificación reenviada");
              } }) : null)));
        })))));
  }

  // ---------- conversation tab ----------

  function bubble(m) {
    const cls = m.role === "user" ? "cand" : m.role === "recruiter" ? "recruiter" : "bot";
    const who = m.role === "user" ? (c.display_name || "Candidato") : m.role === "recruiter" ? `${m.author || "Reclutador"} (reclutador)` : c.profile.agent_name;
    return h("div", { class: `msg ${cls}`, testid: `msg-${cls}` },
      h("div", { class: "msg-meta" }, who, " · ", h("time", { datetime: m.ts, title: m.ts }, fmtDateTime(m.ts))),
      h("div", { class: "msg-body" }, m.content));
  }

  function renderConversation() {
    const transcript = h("div", { class: "transcript", testid: "transcript", "aria-live": "polite" }, c.messages.map(bubble));
    const channelHint = {
      web: "Se entrega en el chat web del candidato.", backoffice: "Se entrega cuando el candidato abra su enlace.",
      whatsapp: "Se envía por WhatsApp.", voice: "Habló por teléfono: el mensaje queda en su historial.",
    }[c.source] || "";
    const text = h("textarea", { rows: 2, placeholder: "Escribe al candidato…  (Ctrl+Enter para enviar)", "aria-label": "Mensaje al candidato", maxlength: 1000 });
    const pause = h("input", { type: "checkbox", checked: true });
    const send = button("Enviar", { icon: "send", variant: "primary", size: "sm", testid: "send-message", onClick: async () => {
      if (!text.value.trim()) return;
      send.disabled = true;
      try {
        await api.post(`/api/bo/candidates/${id}/messages`, { text: text.value, pause_bot: pause.checked });
        text.value = "";
        await reload();
        toast("Mensaje enviado");
      } catch (err) { toast(err.message, "error"); } finally { send.disabled = false; }
    } });
    text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
    const takeover = c.bot_paused
      ? h("div", { class: "banner info", testid: "takeover-banner" },
          h("span", { class: "row" }, icon("pause", 16), h("span", {}, h("strong", {}, "Estás atendiendo esta conversación."), ` ${c.profile.agent_name} está en pausa y no responderá.`)),
          button("Devolver al asistente", { icon: "play", size: "sm", onClick: () => toggleTakeover(false) }))
      : h("div", { class: "banner", testid: "takeover-banner" },
          h("span", { class: "row" }, icon("sparkles", 16), h("span", {}, h("strong", {}, `${c.profile.agent_name} (asistente) atiende esta conversación.`),
            c.status === "active" && c.pending_question ? ` Pregunta pendiente: «${c.pending_question}»` : "")),
          button("Tomar el control", { icon: "pause", size: "sm", onClick: () => toggleTakeover(true) }));
    const reopenSelect = h("select", { "aria-label": "Pregunta a repetir" },
      h("option", { value: "" }, "Automático (siguiente o eliminatoria)"),
      c.profile.questions.map((q) => h("option", { value: q.id }, loc(q.label))));
    clear(panel, takeover,
      h("div", { class: "conv-layout" },
        h("div", { class: "conv-main" },
          c.messages.length ? transcript : emptyState("Sin mensajes todavía.", "message"),
          h("div", { class: "composer-bo" }, text,
            h("div", { class: "row between" },
              h("label", { class: "check small" }, pause, "Pausar al asistente mientras respondo"),
              h("div", { class: "row" }, h("span", { class: "muted small" }, channelHint), send)))),
        h("aside", { class: "panel-card" },
          h("h3", {}, "Reabrir screening"),
          h("p", { class: "muted small", style: { margin: "6px 0 12px" } }, "El asistente retoma la conversación y repite la pregunta elegida (borra la respuesta anterior)."),
          h("div", { class: "stack", style: { gap: "8px" } }, reopenSelect, button("Reabrir y preguntar", { icon: "refresh", testid: "reopen", onClick: async () => {
            try {
              await api.post(`/api/bo/candidates/${id}/reopen`, { question_id: reopenSelect.value || null });
              await reload();
              toast("Screening reabierto");
            } catch (err) { toast(err.message, "error"); }
          } })))));
    transcript.scrollTop = transcript.scrollHeight;
    let count = c.messages.length;
    poll = setInterval(async () => {
      if (document.hidden) return;
      try {
        const fresh = await api.get(`/api/bo/candidates/${id}`);
        if (fresh.messages.length !== count || fresh.bot_paused !== c.bot_paused || fresh.status !== c.status) {
          const typing = document.activeElement === text ? text.value : null;
          c = fresh;
          count = c.messages.length;
          renderHeader();
          renderTabs();
          renderConversation();
          if (typing !== null) { const area = panel.querySelector("textarea"); area.value = typing; area.focus(); }
        }
      } catch { /* transient */ }
    }, 3000);
  }

  async function toggleTakeover(paused) {
    try {
      await api.post(`/api/bo/candidates/${id}/takeover`, { paused });
      await reload();
      toast(paused ? "Has tomado el control de la conversación" : "Conversación devuelta al asistente");
    } catch (err) { toast(err.message, "error"); }
  }

  // ---------- activity tab ----------

  function checkLabel(checkId) {
    return c.report.checks.find((k) => k.id === checkId)?.label || checkId;
  }

  function describe(e) {
    const p = e.payload || {};
    const by = p.by ? `${p.by}` : "Sistema";
    switch (e.type) {
      case "pipeline_change": return [`${by} movió el candidato a «${pipelineLabel(p.to)}»`, p.reason === "stage_deleted" ? "la etapa anterior se eliminó" : `desde «${pipelineLabel(p.from)}»`, "recruiter"];
      case "pipeline_auto": return [`Pasó automáticamente a «${pipelineLabel(p.to)}»`, "según el resultado del screening", "system"];
      case "note_added": return [`${by} añadió una nota`, "", "recruiter"];
      case "note_deleted": return [`${by} borró una nota`, "", "recruiter"];
      case "recruiter_message": return [`${by} envió un mensaje al candidato`, p.pushed ? "entregado por WhatsApp" : "", "recruiter"];
      case "takeover_start": return [`${by} tomó el control de la conversación`, "asistente en pausa", "recruiter"];
      case "takeover_end": return [`${by} devolvió la conversación al asistente`, "", "recruiter"];
      case "recruiter_update": {
        const parts = [];
        if ("assignee" in p) parts.push(`responsable: ${p.assignee || "nadie"}`);
        if ("tags" in p) parts.push(`etiquetas: ${(p.tags || []).join(", ") || "ninguna"}`);
        if ("interview_at" in p) parts.push(`entrevista: ${p.interview_at ? fmtDateTime(p.interview_at) : "sin fecha"}`);
        return [`${by} actualizó la ficha`, parts.join(" · "), "recruiter"];
      }
      case "field_edit": return [`${by} corrigió datos`, Object.entries(p.changes || {}).map(([k, v]) => `${k}: ${JSON.stringify(v.from)} → ${JSON.stringify(v.to)}`).join(" · "), "recruiter"];
      case "recruiter_reopen": return [`${by} reabrió el screening`, `pregunta: ${p.question}`, "recruiter"];
      case "created_by_recruiter": return [`${by} dio de alta al candidato`, "", "recruiter"];
      case "session_close": return [`Screening cerrado: ${p.outcome}`, p.reason ? flagLabel(p.reason) : "", "system"];
      case "session_resume": return ["El candidato retomó la conversación", p.stage ? `en «${p.stage}»` : "", "system"];
      case "rule_triggered": return [`Regla activada: ${flagLabel(p.rule)}`, p.action === "disqualify" ? "eliminatoria" : "alerta", p.action === "disqualify" ? "bad" : "warn"];
      case "needs_review": return ["Necesita revisión", `faltan: ${(p.missing || []).join(", ")}`, "warn"];
      case "reengage_nudge": return [`Recordatorio automático #${p.attempt}`, p.pushed ? "enviado por WhatsApp" : "", "system"];
      case "guardrail": return [`Protección activada: ${p.reason}`, "", "warn"];
      case "candidate_message_during_takeover": return ["El candidato escribió (asistente en pausa)", "", "info"];
      case "give_up_field": return [`Pregunta «${p.stage}» sin validar tras ${p.attempts} intentos`, "", "warn"];
      case "stage_advance": return [`Pregunta «${p.from}» → «${p.to}»`, "", "system"];
      case "check_requested": return [`Verificación solicitada: ${checkLabel(p.check)}`, p.retry_of ? "reintento" : (p.blocking ? "bloqueante" : "no bloqueante"), "info"];
      case "check_result": {
        const labels = { valid: "válido", invalid: "no válido", error: "error" };
        return [`Resultado de ${checkLabel(p.check)}: ${labels[p.result] || p.result}`, [p.detail, p.by ? `por ${p.by}` : ""].filter(Boolean).join(" · "),
                p.result === "valid" ? "recruiter" : p.result === "invalid" ? "bad" : "warn"];
      }
      case "check_retry": return [`${by} reintentó una verificación`, "", "recruiter"];
      default: return [e.type, JSON.stringify(p), "system"];
    }
  }

  function renderActivity() {
    const showTech = h("input", { type: "checkbox" });
    const list = h("ol", { class: "timeline", testid: "timeline" });
    const draw = () => {
      const events = [...c.events].reverse().filter((e) => showTech.checked || !TECHNICAL_EVENTS.has(e.type));
      clear(list, events.length ? events.map((e) => {
        const [title, detail, kind] = describe(e);
        return h("li", { class: `ev ${kind}` }, h("div", { class: "ev-title" }, title),
          detail ? h("div", { class: "muted small" }, detail) : null,
          h("time", { class: "muted small", datetime: e.ts, title: e.ts }, fmtDateTime(e.ts)));
      }) : h("li", { class: "muted" }, "Sin actividad."));
    };
    showTech.addEventListener("change", draw);
    clear(panel, h("section", { class: "panel-card" },
      h("div", { class: "section-title" }, h("h3", {}, "Historial de actividad"),
        h("label", { class: "check small" }, showTech, "Mostrar eventos técnicos del asistente")),
      list));
    draw();
  }

  clear(root, header, tabsEl, panel);
  renderHeader();
  renderTabs();
  renderTab();
  const onChange = (e) => { if (e.detail?.id === id) reload(); };
  window.addEventListener("bo:candidate-changed", onChange);
  return () => { clearInterval(poll); window.removeEventListener("bo:candidate-changed", onChange); };
}
