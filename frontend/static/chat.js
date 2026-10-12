const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const chipEl = $("stage-chip");
let sessionId = null;
let polling = null;
let listening = false;
let recognition = null;
let lastLang = "es";
const SESSION_KEY = "sazon_session_id";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

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

function showTyping() {
  const div = document.createElement("div");
  div.className = "bubble agent typing";
  div.innerHTML = "<span></span><span></span><span></span>";
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function speak(text, lang) {
  if (!$("tts").checked || !window.speechSynthesis || !text) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = (lang || lastLang) === "en" ? "en-US" : "es-ES";
  window.speechSynthesis.speak(u);
}

function setStage(stage, status) {
  chipEl.textContent = (STAGE_LABELS[stage] || stage) + (status && status !== "active" ? " · " + status : "");
  const closed = status && status !== "active";
  $("text").disabled = closed;
  $("send").disabled = closed;
  $("end").disabled = closed;
  $("mic").disabled = closed || !SR;
  if (closed && listening && recognition) recognition.stop();
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
    for (const m of messages) {
      bubble(m.content, "agent nudge");
      speak(m.content, lastLang);
    }
  } catch { /* ignore transient errors */ }
}

async function sendText(text) {
  if (!text || !sessionId) return;
  bubble(text, "user");
  const typing = showTyping();
  $("send").disabled = true;
  try {
    const r = await api(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    typing.remove();
    lastLang = r.language || lastLang;
    if (r.resumed && r.resume_reply) bubble(r.resume_reply, "agent");
    bubble(r.reply, "agent");
    speak(r.reply, r.language);
    setStage(r.stage, r.status);
    if (r.summary) {
      renderOutcome(r.summary);
      clearInterval(polling);
    }
  } catch (e) {
    typing.remove();
    bubble("Error: " + e.message, "system");
  } finally {
    $("send").disabled = $("text").disabled;
  }
}

function startListening() {
  if (!SR) return;
  recognition = new SR();
  recognition.lang = $("mic-lang").value;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => { listening = true; $("mic").classList.add("listening"); };
  recognition.onend = () => { listening = false; $("mic").classList.remove("listening"); };
  recognition.onerror = (e) => { bubble("Error de micrófono: " + e.error, "system"); };
  recognition.onresult = (e) => { sendText(e.results[0][0].transcript.trim()); };
  recognition.start();
}

$("composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("text");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendText(text);
});

$("mic").addEventListener("click", () => {
  if (listening) { recognition.stop(); return; }
  startListening();
});

$("mic-lang").addEventListener("change", () => {
  if (listening && recognition) { recognition.stop(); startListening(); }
});

$("end").addEventListener("click", async () => {
  if (!sessionId) return;
  $("end").disabled = true;
  try {
    const r = await api(`/api/sessions/${sessionId}/end`, { method: "POST" });
    if (r.reply) { bubble(r.reply, "agent"); speak(r.reply, lastLang); }
    setStage("abandoned", "abandoned");
    if (r.summary) {
      renderOutcome(r.summary);
      clearInterval(polling);
    }
    if (r.status === "abandoned") $("resume-banner").hidden = false;
  } catch (e) {
    bubble("Error: " + e.message, "system");
  }
});

$("resume").addEventListener("click", async () => {
  if (!sessionId) return;
  $("resume").disabled = true;
  try {
    const r = await api(`/api/sessions/${sessionId}/resume`, { method: "POST" });
    $("resume-banner").hidden = true;
    lastLang = r.language || lastLang;
    bubble(r.reply, "agent");
    speak(r.reply, lastLang);
    setStage(r.stage, r.status);
    if (!polling) polling = setInterval(pollPending, 15000);
  } catch (e) {
    bubble("No se pudo retomar: " + e.message, "system");
    $("resume").disabled = false;
  }
});

function renderHistory(messages) {
  for (const m of messages) {
    if (m.role === "assistant") bubble(m.content, m.delivered ? "agent" : "agent nudge");
    else if (m.role === "user") bubble(m.content, "user");
  }
}

async function restoreSession(id) {
  const s = await api(`/api/sessions/${id}`);
  sessionId = s.session_id || id;
  lastLang = s.language || "es";
  renderHistory(s.messages || []);
  setStage(s.stage, s.status);
  if (s.summary) renderOutcome(s.summary);
  if (s.status === "abandoned") $("resume-banner").hidden = false;
  polling = setInterval(pollPending, 15000);
}

async function startFresh() {
  const s = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "web" }),
  });
  sessionId = s.session_id;
  localStorage.setItem(SESSION_KEY, sessionId);
  lastLang = s.language || "es";
  bubble(s.reply, "agent");
  speak(s.reply, lastLang);
  setStage(s.stage, "active");
  polling = setInterval(pollPending, 15000);
}

async function init() {
  const saved = localStorage.getItem(SESSION_KEY);
  if (saved) {
    try {
      await restoreSession(saved);
      return;
    } catch {
      localStorage.removeItem(SESSION_KEY);
    }
  }
  try {
    await startFresh();
  } catch (e) {
    bubble("No se pudo conectar con el agente: " + e.message, "system");
  }
}

init();
