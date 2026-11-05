// Screening analytics. The filters at the top (process, period, channel,
// language) drive the numbers, and every number links to the candidate list
// with the same filters plus the clicked segment.

import { api, qs } from "../api.js";
import { clear, fmtDuration, h, icon, loc, segmented } from "../kit.js";
import { faqLabel, flagLabel, loadMeta, navigate, pipelineLabel, profileById, sourceLabel, stageTone, store } from "../shared.js";

const PERIODS = [["7", "7 días"], ["30", "30 días"], ["90", "90 días"], ["all", "Todo"]];

function isoDaysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export async function renderAnalytics(root, params) {
  const meta = await loadMeta();
  const f = {
    period: params.get("period") || "all",
    source: params.get("source") || "",
    language: params.get("language") || "",
  };

  const base = () => ({
    profile_id: store.profile,
    started_from: f.period === "all" ? "" : isoDaysAgo(Number(f.period)),
    source: f.source,
    language: f.language,
  });
  const toCandidates = (extra) => navigate("candidates", { ...base(), ...extra });

  const body = h("div", { testid: "analytics-body" });
  const source = h("select", { "aria-label": "Canal" }, h("option", { value: "" }, "Todos los canales"),
    Object.keys(meta.sources).map((s) => h("option", { value: s, selected: s === f.source }, sourceLabel(s))));
  const language = h("select", { "aria-label": "Idioma" }, [["", "Todos los idiomas"], ["es", "Español"], ["en", "English"]]
    .map(([v, l]) => h("option", { value: v, selected: v === f.language }, l)));
  const update = () => {
    history.replaceState(null, "", `#/analytics?${qs({ period: f.period === "all" ? "" : f.period, source: f.source, language: f.language })}`);
    load();
  };
  source.addEventListener("change", () => { f.source = source.value; update(); });
  language.addEventListener("change", () => { f.language = language.value; update(); });
  const profile = profileById(store.profile);

  clear(root,
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Analítica"),
        h("p", { class: "muted" }, profile ? `${loc(profile.role)} · ${profile.company}` : "Todos los procesos", " · haz clic en cualquier cifra para ver esos candidatos")),
      h("div", { class: "toolbar" }, segmented(PERIODS, f.period, (v) => { f.period = v; update(); }, "Periodo"), source, language)),
    body);

  function stat(value, label, extra, testid) {
    const content = [h("div", { class: "stat-num" }, String(value)),
      h("div", { class: "stat-lbl" }, h("span", {}, label), extra ? icon("arrowRight", 14, "stat-go") : null)];
    return extra
      ? h("button", { type: "button", class: "stat", testid, "aria-label": `${label}: ${value}. Ver candidatos`, onclick: () => toCandidates(extra) }, content)
      : h("div", { class: "stat", testid }, content);
  }

  function chart(title, data, labeler, toFilter, { tone = "", note = "" } = {}) {
    const entries = Object.entries(data || {}).filter(([, v]) => v > 0 || tone === "all");
    const max = Math.max(1, ...entries.map(([, v]) => v));
    return h("section", { class: "panel-card", testid: `chart-${title}` },
      h("div", { class: "section-title" }, h("div", {}, h("h3", {}, title), note ? h("p", { class: "muted small" }, note) : null)),
      entries.length ? h("div", { class: `chart-rows ${tone}` }, entries.map(([k, v]) => h("button", {
        type: "button", class: "chart-row", "aria-label": `${labeler(k)}: ${v}. Ver candidatos`, onclick: () => toCandidates(toFilter(k)),
      }, h("span", { class: "lbl" }, labeler(k)),
      h("span", { class: "bar" }, h("span", { style: { width: `${(100 * v) / max}%`, background: tone === "stages" ? `var(--c-${stageTone(k)})` : null } })),
      h("span", { class: "num" }, String(v)), icon("arrowRight", 14)))) : h("p", { class: "muted small" }, "Sin datos en este periodo."));
  }

  async function load() {
    const a = await api.get(`/api/bo/analytics?${qs(base())}`);
    const questionLabel = (id) => {
      for (const p of profile ? [profile] : meta.profiles) {
        const q = p.questions.find((x) => x.id === id);
        if (q) return loc(q.label);
      }
      return { intro: "Inicio", review: "Final de preguntas", qualified: "Pre-calificados" }[id] || id;
    };
    clear(body,
      h("div", { class: "stats" },
        stat(a.total_sessions, "Candidatos", {}, "stat-total"),
        stat(a.active_sessions, "En conversación", { status: "active" }),
        stat(`${a.qualified_count}`, `Pre-calificados (${a.qualified_rate_pct}%)`, { status: "qualified" }, "stat-qualified"),
        stat(a.disqualified_count, "No califican", { status: "disqualified" }),
        stat(a.abandoned_count, "Incompletos", { status: "abandoned" }),
        a.verifying_count ? stat(a.verifying_count, "En verificación", { status: "verifying" }) : null,
        stat(a.avg_score ?? "—", "Puntuación media", { sort: "score", order: "desc" }),
        stat(fmtDuration(a.avg_duration_seconds), "Duración media"),
        stat(`${a.completion_rate_pct}%`, "Tasa de finalización")),
      h("div", { class: "chart-grid" },
        profile
          ? chart("Embudo por pregunta", a.funnel, questionLabel, (k) => ({ reached: k }), { tone: "all", note: "Candidatos que respondieron cada pregunta" })
          : h("section", { class: "panel-card" }, h("h3", {}, "Embudo por pregunta"),
              h("p", { class: "muted small", style: { marginTop: "6px" } }, "Cada proceso tiene sus propias preguntas: elige un proceso arriba para ver su embudo.")),
        chart("Candidatos por etapa", a.pipeline, pipelineLabel, (k) => ({ pipeline_stage: k }), { tone: "stages" }),
        chart("Abandono por pregunta", a.dropoff_by_stage, questionLabel, (k) => ({ status: "abandoned", dropped_at: k }), { tone: "warn" }),
        chart("Motivos de descarte", a.disqualify_reasons, flagLabel, (k) => ({ status: "disqualified", disqualify_reason: k }), { tone: "bad" }),
        chart("Alertas más frecuentes", a.flag_counts, flagLabel, (k) => ({ flag: k }), { tone: "warn" }),
        chart("Idioma de la conversación", a.languages, (k) => ({ es: "Español", en: "English" }[k] || k), (k) => ({ language: k })),
        chart("Preguntas frecuentes más consultadas", a.top_rag_hits, faqLabel, (k) => ({ faq: k })),
        profile ? null : chart("Candidatos por proceso", a.by_profile, (k) => { const p = profileById(k); return p ? loc(p.role) : k; },
          (k) => ({ profile_id: k }))));
  }

  await load();
  return () => {};
}
