const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const chipEl = $("stage-chip");
let sessionId = null;
let polling = null;

const STAGE_LABELS = {
  intro: "Inicio", name: "Nombre", license: "Licencia de conducir",
  location: "Ciudad / zona", availability: "Disponibilidad", schedule: "Horario",
  experience: "Experiencia", start_date: "Fecha de inicio", review: "Revisión",
  qualified: "Pre-calificado", disqualified: "Descalificado", abandoned: "Abandonado",
};

function bubble(text, cls) {
  const div = document.createElement("div");
  div.className = "bubble " + cls;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function setStage(stage, status) {
  chipEl.textContent = (STAGE_LABELS[stage] || stage) + (status && status !== "active" ? " · " + status : "");
  const closed = status && status !== "active";
  $("text").disabled = closed;
  $("send").disabled = closed;
  $("end").disabled = closed;
}

function renderOutcome(summary) {
  if (!summary) return;
  const cls = summary.qualified ? "qualified" : summary.outcome === "disqualified" ? "disqualified" : "abandoned";
  const label = summary.qualified ? "Pre-calificado para entrevista" : summary.outcome === "disqualified" ? "No califica para el puesto" : "Conversación incompleta";
  const el = $("outcome");
  el.innerHTML = "";
  const box = document.createElement("div");
  box.className = "outcome";
  const h = document.createElement("h3");
  const tag = document.createElement("span");
  tag.className = "tag " + cls;
  tag.textContent = label;
  h.appendChild(tag);
  box.appendChild(h);
  if (summary.recruiter_note) {
    const p = document.createElement("div");
    p.style.color = "var(--muted)";
    p.style.fontSize = ".88rem";
    p.textContent = summary.recruiter_note;
    box.appendChild(p);
  }
  el.appendChild(box);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

async function pollPending() {
  if (!sessionId) return;
  try {
    const { messages } = await api(`/api/sessions/${sessionId}/pending`);
    for (const m of messages) bubble(m.content, "agent nudge");
  } catch { /* ignore transient errors */ }
}

async function init() {
  try {
    const s = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "web" }),
    });
    sessionId = s.session_id;
    bubble(s.reply, "agent");
    setStage(s.stage, "active");
    polling = setInterval(pollPending, 15000);
  } catch (e) {
    bubble("No se pudo conectar con el agente: " + e.message, "system");
  }
}

$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("text");
  const text = input.value.trim();
  if (!text || !sessionId) return;
  input.value = "";
  bubble(text, "user");
  $("send").disabled = true;
  try {
    const r = await api(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (r.validation_error) bubble("…", "system");
    bubble(r.reply, "agent");
    setStage(r.stage, r.status);
    if (r.summary) {
      renderOutcome(r.summary);
      clearInterval(polling);
    }
  } catch (e) {
    bubble("Error: " + e.message, "system");
  } finally {
    $("send").disabled = $("text").disabled;
  }
});

$("end").addEventListener("click", async () => {
  if (!sessionId) return;
  $("end").disabled = true;
  try {
    const r = await api(`/api/sessions/${sessionId}/end`, { method: "POST" });
    if (r.reply) bubble(r.reply, "agent");
    setStage("abandoned", "abandoned");
    if (r.summary) {
      renderOutcome(r.summary);
      clearInterval(polling);
    }
  } catch (e) {
    bubble("Error: " + e.message, "system");
  }
});

init();
