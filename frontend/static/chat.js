const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const chipEl = $("stage-chip");
let sessionId = null;
let profile = null;          // active job profile (id, company, role, agent_name, questions)
let profiles = [];
let polling = null;
let listening = false;
let recognition = null;
let lastLang = "es";
let status = "active";
let botPaused = false;
let pausedNoticeShown = false;

const LEGACY_SESSION_KEY = "sazon_session_id";
const sessionKey = (profileId) => `screening_session:${profileId}`;
const params = new URLSearchParams(location.search);

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

const LIFECYCLE_LABELS = {
  intro: "Inicio", review: "Revisión", verifying: "Verificando tus datos…",
  qualified: "Pre-calificado", disqualified: "Descalificado", abandoned: "Abandonado",
};

const loc = (text, lang = "es") => (typeof text === "string" ? text : (text && (text[lang] || text.es || text.en)) || "");

function bubble(text, cls, label) {
  const div = document.createElement("div");
  div.className = "bubble " + cls;
  if (label) {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = label;
    div.appendChild(who);
  }
  const body = document.createElement("div");
  body.textContent = text;
  div.appendChild(body);
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function recruiterBubble(text, author) {
  return bubble(text, "recruiter", `${author || "Reclutador/a"} · ${lastLang === "en" ? "recruiter" : "equipo de selección"}`);
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

function stageLabel(stage) {
  const questions = (profile && profile.questions) || [];
  const idx = questions.findIndex((q) => q.id === stage);
  if (idx >= 0) return `${loc(questions[idx].label, lastLang)} · ${idx + 1}/${questions.length}`;
  return LIFECYCLE_LABELS[stage] || stage;
}

function setStage(stage, newStatus, paused) {
  if (newStatus) status = newStatus;
  if (paused !== undefined) botPaused = paused;
  chipEl.textContent = botPaused
    ? (lastLang === "en" ? "Chatting with a recruiter" : "Hablando con el equipo de selección")
    : stageLabel(stage) + (status && status !== "active" ? " · " + status : "");
  // a recruiter can keep talking to a candidate after the screening closed
  const closed = status && status !== "active" && !botPaused;
  $("text").disabled = closed;
  $("send").disabled = closed;
  $("end").disabled = closed || botPaused;
  $("mic").disabled = closed || !SR;
  if (closed && listening && recognition) recognition.stop();
}

function renderOutcome(summary) {
  if (!summary) return;
  const verifying = summary.outcome === "verifying";
  const cls = summary.qualified ? "qualified" : summary.outcome === "disqualified" ? "disqualified" : verifying ? "verifying" : "abandoned";
  const label = summary.qualified ? "Pre-calificado para entrevista" : summary.outcome === "disqualified" ? "No califica para el puesto"
    : verifying ? (lastLang === "en" ? "Verifying your details…" : "Verificando tus datos…") : "Conversación incompleta";
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
  el.appendChild(box);
}

function applyBranding() {
  if (!profile) return;
  const brand = $("brand");
  brand.textContent = "";
  brand.append(profile.company + " ");
  const span = document.createElement("span");
  span.textContent = "· Empleo";
  brand.appendChild(span);
  $("role").textContent = `${loc(profile.role, lastLang)} — ${lastLang === "en" ? "assistant" : "asistente"} ${profile.agent_name}`;
  document.title = `${profile.company} — ${loc(profile.role, lastLang)}`;
  $("change-job").hidden = profiles.length < 2;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

async function pollPending() {
  if (!sessionId) return;
  try {
    const { messages, session } = await api(`/api/sessions/${sessionId}/pending`);
    for (const m of messages) {
      if (m.role === "recruiter") recruiterBubble(m.content, m.author);
      else bubble(m.content, "agent nudge");
      speak(m.content, lastLang);
    }
    if (session) {
      lastLang = session.language || lastLang;
      const reopened = session.status === "active" && status !== "active";
      const decided = status === "verifying" && session.status !== "verifying";
      setStage(session.stage, session.status, session.bot_paused);
      if (decided) {
        // the external checks answered: show the final decision
        const full = await api(`/api/sessions/${sessionId}`).catch(() => null);
        if (full && full.summary) renderOutcome(full.summary);
      }
      if (reopened) {
        $("outcome").innerHTML = "";
        $("resume-banner").hidden = true;
      }
      if (!session.bot_paused) pausedNoticeShown = false;
    }
  } catch { /* ignore transient errors */ }
}

function startPolling() {
  clearInterval(polling);
  polling = setInterval(pollPending, 4000);
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
    if (r.reply) {
      bubble(r.reply, "agent");
      speak(r.reply, r.language);
    } else if (r.paused && !pausedNoticeShown) {
      bubble(lastLang === "en" ? "A recruiter will reply to you here shortly." : "Una persona del equipo de selección te responderá aquí en breve.", "system");
      pausedNoticeShown = true;
    }
    setStage(r.stage, r.status, r.bot_paused);
    if (r.summary && !r.bot_paused) renderOutcome(r.summary);
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
    setStage("abandoned", r.status || "abandoned");
    if (r.summary) renderOutcome(r.summary);
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
    $("outcome").innerHTML = "";
    lastLang = r.language || lastLang;
    bubble(r.reply, "agent");
    speak(r.reply, lastLang);
    setStage(r.stage, r.status);
    startPolling();
  } catch (e) {
    bubble("No se pudo retomar: " + e.message, "system");
    $("resume").disabled = false;
  }
});

function renderHistory(messages) {
  for (const m of messages) {
    if (m.role === "assistant") bubble(m.content, m.delivered ? "agent" : "agent nudge");
    else if (m.role === "recruiter") recruiterBubble(m.content, m.author);
    else if (m.role === "user") bubble(m.content, "user");
  }
}

function showChat() {
  $("job-picker").hidden = true;
  $("chat").hidden = false;
}

async function restoreSession(id) {
  const s = await api(`/api/sessions/${id}`);
  sessionId = s.id || id;
  profile = profiles.find((p) => p.id === s.profile_id) || (s.profile ? { ...s.profile, questions: [] } : null);
  lastLang = s.language || "es";
  localStorage.setItem(sessionKey(s.profile_id), sessionId);
  applyBranding();
  showChat();
  renderHistory(s.messages || []);
  setStage(s.stage, s.status, !!s.bot_paused);
  if (s.summary && !s.bot_paused) renderOutcome(s.summary);
  if (s.status === "abandoned" && !s.bot_paused) $("resume-banner").hidden = false;
  // undelivered messages were just rendered from history: mark them delivered
  await api(`/api/sessions/${sessionId}/pending`).catch(() => {});
  startPolling();
}

async function startFresh(profileId) {
  const s = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "web", profile_id: profileId }),
  });
  sessionId = s.session_id;
  profile = profiles.find((p) => p.id === s.profile_id) || profile;
  localStorage.setItem(sessionKey(s.profile_id), sessionId);
  lastLang = s.language || "es";
  applyBranding();
  showChat();
  bubble(s.reply, "agent");
  speak(s.reply, lastLang);
  setStage(s.stage, "active", false);
  startPolling();
}

async function openJob(profileId) {
  const saved = localStorage.getItem(sessionKey(profileId))
    || (profileId === "delivery_courier" ? localStorage.getItem(LEGACY_SESSION_KEY) : null);
  if (saved) {
    try {
      await restoreSession(saved);
      return;
    } catch {
      localStorage.removeItem(sessionKey(profileId));
    }
  }
  await startFresh(profileId);
}

function showPicker() {
  $("chat").hidden = true;
  $("job-picker").hidden = false;
  const jobs = $("jobs");
  jobs.innerHTML = "";
  for (const p of profiles) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "job";
    card.dataset.job = p.id;
    const title = document.createElement("strong");
    title.textContent = loc(p.role);
    const company = document.createElement("span");
    company.className = "company";
    company.textContent = p.company;
    const summary = document.createElement("span");
    summary.className = "summary";
    summary.textContent = loc(p.summary);
    card.append(title, company, summary);
    card.addEventListener("click", () => {
      history.replaceState(null, "", `/?job=${encodeURIComponent(p.id)}`);
      openJob(p.id).catch((e) => bubble("No se pudo conectar con el agente: " + e.message, "system"));
    });
    jobs.appendChild(card);
  }
}

async function init() {
  try {
    const data = await api("/api/profiles");
    profiles = data.profiles || [];
    const job = params.get("job");
    const linked = params.get("session");
    if (linked) {
      await restoreSession(linked);
    } else if (job && profiles.some((p) => p.id === job)) {
      await openJob(job);
    } else if (profiles.length === 1) {
      await openJob(profiles[0].id);
    } else {
      showPicker();
    }
  } catch (e) {
    showChat();
    bubble("No se pudo conectar con el agente: " + e.message, "system");
  }
}

init();
