// Channel simulator UI: virtual phones that talk to the screening app the way Twilio
// (signed webhooks + TwiML) or Meta's Cloud API (signed JSON + Graph API) do, plus the
// API traffic the app sends back.
// State comes from GET /sim/api/state (long-poll); actions are POSTs to /sim/api.

const $ = (sel) => document.querySelector(sel);
let state = null;
let selected = localStorage.getItem("sim_phone") || null;
let spokenSeq = 0;
const busy = new Set();

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, body, method = "POST") {
  const res = await fetch(path, {
    method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

const time = (iso) => new Date(iso).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const currentPhone = () => state?.phones.find((p) => p.number === selected) || null;
const activeCall = (phone) => phone && [...phone.calls].reverse().find((c) => c.state !== "completed");

function keepScroll(list, render) {
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  render();
  if (atBottom) list.scrollTop = list.scrollHeight;
}

// ---------- rendering ----------

function render() {
  if (!state) return;
  const { config } = state;
  const provider = config.whatsapp_provider === "meta" ? `Meta Cloud API (id ${config.meta_phone_number_id})` : "Twilio";
  const voice = { twilio: "Twilio", vonage: "Vonage" }[config.voice_provider] || config.voice_provider;
  $("#config").textContent = `WhatsApp ${config.whatsapp_number} vía ${provider} · Voz ${config.voice_number} vía ${voice} · ` +
    `App ${config.app_url}` + (config.signing ? " · webhooks firmados" : " · sin firma");
  for (const node of document.querySelectorAll("[data-provider]")) node.hidden = node.dataset.provider !== config.whatsapp_provider;
  $("#backoffice-link").href = `${config.app_url}/backoffice`;
  if (!selected && state.phones.length) selected = state.phones[0].number;
  renderPhones();
  renderFaults();
  const phone = currentPhone();
  $("#no-phone").hidden = !!phone;
  $("#wa").hidden = !phone;
  $("#voice").hidden = !phone;
  if (phone) {
    renderWhatsApp(phone);
    renderCall(phone);
  }
  renderLog(phone);
}

function renderPhones() {
  const list = $("#phone-list");
  list.replaceChildren(...state.phones.map((p) => {
    const call = activeCall(p);
    return el("li", { class: p.number === selected ? "selected" : "", "data-number": p.number,
                      onclick: () => select(p.number) },
      el("span", {}, el("strong", {}, p.name || p.number), p.name ? el("div", { class: "muted small" }, p.number) : null),
      el("span", { class: "badges" },
        call ? el("span", { class: "pill ok", title: "En llamada" }, "📞") : null,
        p.thread.length ? el("span", { class: "pill", title: "Mensajes de WhatsApp" }, p.thread.length) : null));
  }));
}

function renderFaults() {
  for (const input of document.querySelectorAll("[data-fault]")) {
    if (document.activeElement === input) continue;
    const value = state.faults[input.dataset.fault];
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value;
  }
}

const VIA = { twiml: "TwiML", rest: "API REST", graph: "API Graph", template: "Plantilla", webhook: "" };

function renderWhatsApp(phone) {
  $("#wa-title").textContent = `${phone.name ? phone.name + " · " : ""}${phone.number}`;
  const windowPill = $("#wa-window");
  windowPill.textContent = phone.window_open ? "ventana 24 h abierta" : "ventana 24 h cerrada";
  windowPill.className = `pill ${phone.window_open ? "ok" : "bad"}`;
  const thread = $("#wa-thread");
  keepScroll(thread, () => thread.replaceChildren(...phone.thread.map((m) => {
    const failed = m.status !== "delivered";
    const content = [];
    if (m.media_id && (m.media_mime || "").startsWith("audio/")) {
      content.push(el("audio", { controls: true, preload: "none", src: `/sim/media/${m.media_id}` }));
      content.push(el("div", { class: "transcript-note" }, m.transcript ? `🎤 “${m.transcript}”` : "🎤 audio grabado"));
    } else if (m.media_id || m.media?.length) {
      content.push(el("div", { class: "transcript-note" }, "📷 Foto"));
    }
    if (m.body) content.push(el("div", {}, m.body));
    if (!content.length) content.push("(vacío)");
    const ticks = m.sender === "business" ? (failed ? "" : m.read ? " ✓✓ leído" : " ✓✓")
      : (m.read ? " ✓✓ leído" : " ✓");
    return el("li", { class: `msg ${m.sender}${failed ? " failed" : ""}`, "data-via": m.via },
      ...content,
      el("span", { class: "meta" },
        VIA[m.via] ? el("span", { class: "via" }, VIA[m.via], " · ") : null,
        failed ? `no entregado (error ${m.error_code}) · ` : null,
        time(m.at), el("span", { class: m.read ? "ticks read" : "ticks" }, ticks)));
  })));
}

function renderCall(phone) {
  const call = activeCall(phone);
  const last = call || phone.calls[phone.calls.length - 1];
  $("#call-title").textContent = `${phone.number} → ${state.config.voice_number}`;  // caller → our number
  const transcript = $("#call-transcript");
  keepScroll(transcript, () => transcript.replaceChildren(...(last ? last.transcript : []).map((u) =>
    el("li", { class: `line ${u.speaker}` }, u.language ? el("span", { class: "lang" }, u.language) : null, u.text))));

  const status = $("#call-status");
  if (!call) {
    status.className = "call-status";
    status.textContent = last ? `Llamada terminada (${endLabel(last.end_reason)})` : "Sin llamada en curso";
  } else if (call.waiting) {
    status.className = "call-status listening";
    const who = { twilio: "Twilio", vonage: "Vonage" }[call.provider] || call.provider || "El proveedor";
    status.textContent = `${who} escucha (${call.gather_language || "?"})…`;
  } else {
    status.className = "call-status";
    status.textContent = "Conectando…";
  }
  const inFlight = busy.has(`call:${phone.number}`);
  $("#dial").disabled = !!call || inFlight;
  $("#speak").disabled = !call?.waiting || inFlight;
  $("#silence").disabled = !call?.waiting || inFlight;
  $("#hangup").disabled = !call;
  $("#call-text").disabled = !call?.waiting;
  const mic = $("#mic");
  mic.hidden = !recognitionClass();
  mic.disabled = !call?.waiting || inFlight;

  const past = phone.calls.filter((c) => c !== last);
  $("#past-calls").hidden = !past.length;
  $("#past-calls-list").replaceChildren(...past.reverse().map((c) => el("div", { class: "past" },
    el("strong", {}, `${time(c.started_at)} · ${endLabel(c.end_reason)}`),
    el("ol", {}, c.transcript.map((u) => el("li", {}, `${u.speaker}: ${u.text}`))))));
  speakNew(call);
}

function endLabel(reason) {
  return { "hangup-by-app": "colgó el asistente", "caller-hangup": "colgó el candidato",
           "end-of-twiml": "fin del TwiML", "application-error": "error de la aplicación" }[reason] || reason || "";
}

function renderLog(phone) {
  const phoneOnly = $("#log-phone-only").checked;
  const entries = state.log.filter((e) => !phoneOnly || !phone || !e.phone || e.phone === phone.number).reverse();
  const open = new Set([...document.querySelectorAll("#log details[open]")].map((d) => d.dataset.seq));
  $("#log").replaceChildren(...entries.map((e) => {
    const path = e.url.replace(/^https?:\/\/[^/]+/, "");
    return el("li", {}, el("details", { "data-seq": e.seq, open: open.has(String(e.seq)) },
      el("summary", {},
        el("span", { class: "kind" }, { webhook: "→ app", status_callback: "→ app (estado)", rest: "← REST", note: "ℹ" }[e.kind] || e.kind),
        el("span", {}, e.method, " ", path || e.response),
        e.status != null ? el("span", { class: e.status >= 400 ? "err" : "" }, e.status) : null,
        e.elapsed_ms != null ? el("span", { class: "muted" }, `${e.elapsed_ms} ms`) : null,
        e.error ? el("span", { class: "err" }, e.error) : null),
      e.kind === "note" ? null : el("pre", {}, JSON.stringify(e.params, null, 1)),
      e.kind === "note" ? null : el("pre", {}, e.response || "(sin cuerpo)")));
  }));
}

// ---------- text to speech / speech to text (browser, optional) ----------

function speakNew(call) {
  if (!call) return;
  const fresh = call.transcript.filter((u) => u.speaker === "agent" && u.seq > spokenSeq);
  if (!fresh.length) return;
  spokenSeq = Math.max(...fresh.map((u) => u.seq));
  if (!$("#tts").checked || !window.speechSynthesis) return;
  for (const u of fresh) {
    const utterance = new SpeechSynthesisUtterance(u.text);
    utterance.lang = u.language || "es-ES";
    speechSynthesis.speak(utterance);
  }
}

const recognitionClass = () => window.SpeechRecognition || window.webkitSpeechRecognition;

function dictate() {
  const call = activeCall(currentPhone());
  const Recognition = recognitionClass();
  if (!call?.waiting || !Recognition) return;
  const rec = new Recognition();
  rec.lang = call.gather_language || "es-ES";
  rec.interimResults = false;
  rec.onresult = (event) => {
    $("#call-text").value = event.results[0][0].transcript;
    $("#call-form").requestSubmit();
  };
  rec.onerror = (event) => toast(`Micrófono: ${event.error}`);
  rec.start();
  toast(`Escuchando (${rec.lang})…`);
}

// ---------- actions ----------

function select(number) {
  selected = number;
  localStorage.setItem("sim_phone", number);
  render();
}

async function run(key, fn) {
  busy.add(key);
  render();
  try {
    return await fn();
  } catch (err) {
    toast(err.message);
  } finally {
    busy.delete(key);
    render();
  }
}

$("#add-phone").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const phone = await api("/sim/api/phones", { number: form.number.value, name: form.name.value });
    form.reset();
    select(phone.number);
  } catch (err) { toast(err.message); }
});

$("#random-phone").addEventListener("click", () => {
  const form = $("#add-phone");
  form.number.value = `+346${String(Math.floor(Math.random() * 1e8)).padStart(8, "0")}`;
  form.requestSubmit();
});

$("#wa-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = $("#wa-text");
  const body = box.value.trim();
  const phone = currentPhone();
  if (!body || !phone) return;
  box.value = "";
  // no await: the candidate may keep typing while the app answers (Twilio delivers webhooks concurrently)
  api(`/sim/api/phones/${encodeURIComponent(phone.number)}/whatsapp`, { body }).catch((err) => toast(err.message));
});

$("#wa-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#wa-form").requestSubmit();
  }
});

$("#wa-media").addEventListener("click", () => {
  const phone = currentPhone();
  if (!phone) return;
  const caption = $("#wa-text").value.trim();
  $("#wa-text").value = "";
  api(`/sim/api/phones/${encodeURIComponent(phone.number)}/photo`, { caption }).catch((err) => toast(err.message));
});

$("#wa-voice").addEventListener("click", () => {
  const phone = currentPhone();
  const text = $("#wa-text").value.trim();
  if (!phone) return;
  if (!text) { toast("Escribe lo que dices en la nota de voz y pulsa 🎤"); return; }
  $("#wa-text").value = "";
  api(`/sim/api/phones/${encodeURIComponent(phone.number)}/voice-note`, { text, language: $("#wa-lang").value })
    .catch((err) => toast(err.message));
});

// Real recording: only a real speech-to-text service in the app can understand it.
let recorder = null;
if (window.MediaRecorder && navigator.mediaDevices?.getUserMedia) $("#wa-record").hidden = false;
$("#wa-record").addEventListener("click", async () => {
  const phone = currentPhone();
  if (!phone) return;
  if (recorder) { recorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      recorder = null;
      $("#wa-record").textContent = "⏺";
      await fetch(`/sim/api/phones/${encodeURIComponent(phone.number)}/audio`,
                  { method: "POST", headers: { "Content-Type": blob.type }, body: blob }).catch((err) => toast(err.message));
    };
    recorder.start();
    $("#wa-record").textContent = "⏹";
    toast("Grabando… pulsa ⏹ para enviar");
  } catch (err) { toast(`Micrófono: ${err.message}`); }
});

$("#meta-verify").addEventListener("click", async () => {
  const r = await api("/sim/api/meta/verify", {}).catch((err) => ({ ok: false, body: err.message }));
  toast(r.ok ? "Webhook verificado: la app devolvió hub.challenge" : `Verificación fallida (${r.status}): ${r.body}`);
});

$("#close-window").addEventListener("click", () => {
  const phone = currentPhone();
  if (phone) api(`/sim/api/phones/${encodeURIComponent(phone.number)}/window`, {}).catch((err) => toast(err.message));
});

$("#dial").addEventListener("click", () => {
  const phone = currentPhone();
  if (phone) run(`call:${phone.number}`, () => api(`/sim/api/phones/${encodeURIComponent(phone.number)}/calls`, {}));
});

$("#call-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const phone = currentPhone();
  const call = activeCall(phone);
  const text = $("#call-text").value.trim();
  if (!call || !text) return;
  $("#call-text").value = "";
  run(`call:${phone.number}`, () => api(`/sim/api/calls/${call.sid}/speech`, { text }))
    .then(() => $("#call-text").focus());
});

$("#silence").addEventListener("click", () => {
  const phone = currentPhone();
  const call = activeCall(phone);
  if (call) run(`call:${phone.number}`, () => api(`/sim/api/calls/${call.sid}/silence`, {}));
});

$("#hangup").addEventListener("click", () => {
  const call = activeCall(currentPhone());
  if (call) api(`/sim/api/calls/${call.sid}/hangup`, {}).catch((err) => toast(err.message));
});

$("#mic").addEventListener("click", dictate);
$("#log-phone-only").addEventListener("change", render);

for (const input of document.querySelectorAll("[data-fault]")) {
  input.addEventListener("change", () => {
    const key = input.dataset.fault;
    const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    api("/sim/api/faults", { [key]: value }).catch((err) => toast(err.message));
  });
}

$("#reset").addEventListener("click", async () => {
  if (!confirm("¿Borrar todos los teléfonos, mensajes y llamadas del simulador? (la app no se toca)")) return;
  await api("/sim/api/reset", {});
  selected = null;
  localStorage.removeItem("sim_phone");
});

// ---------- state loop ----------

async function poll() {
  for (;;) {
    try {
      const version = state ? state.version : -1;
      const res = await fetch(`/sim/api/state?version=${version}&wait=20`);
      state = await res.json();
      if (selected && !state.phones.some((p) => p.number === selected)) selected = null;
      render();
    } catch {
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
}

poll();
