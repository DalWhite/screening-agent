const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const statusEl = $("status");
let sessionId = null;
let listening = false;
let recognition = null;

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

function bubble(text, cls) {
  const div = document.createElement("div");
  div.className = "bubble " + cls;
  div.textContent = text;
  messagesEl.appendChild(div);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function speak(text, lang) {
  if (!$("tts").checked || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = lang || "es-ES";
  window.speechSynthesis.speak(u);
}

async function ensureSession() {
  if (sessionId) return;
  const s = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "voice", language: $("lang").value.startsWith("en") ? "en" : "es" }),
  });
  sessionId = s.session_id;
  bubble(s.reply, "agent");
  speak(s.reply, $("lang").value);
}

function handleReply(r) {
  bubble(r.reply, "agent");
  speak(r.reply, $("lang").value);
  if (r.summary) statusEl.textContent = "Conversación cerrada: " + r.status;
}

function startListening() {
  if (!SR) {
    statusEl.textContent = "Tu navegador no soporta Web Speech API (usa Chrome/Edge).";
    return;
  }
  recognition = new SR();
  recognition.lang = $("lang").value;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => { listening = true; $("mic").classList.add("listening"); statusEl.textContent = "Escuchando…"; };
  recognition.onend = () => {
    listening = false;
    $("mic").classList.remove("listening");
    if (statusEl.textContent === "Escuchando…") statusEl.textContent = "Listo para escuchar.";
  };
  recognition.onerror = (e) => { statusEl.textContent = "Error de micrófono: " + e.error; };
  recognition.onresult = async (e) => {
    const text = e.results[0][0].transcript;
    bubble(text, "user");
    try {
      const r = await api(`/api/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      handleReply(r);
    } catch (err) {
      bubble("Error: " + err.message, "system");
    }
  };
  recognition.start();
}

$("mic").addEventListener("click", async () => {
  if (listening) { recognition.stop(); return; }
  try {
    await ensureSession();
    startListening();
  } catch (e) {
    statusEl.textContent = "No se pudo iniciar: " + e.message;
  }
});

$("lang").addEventListener("change", () => {
  if (listening) { recognition.stop(); startListening(); }
});
