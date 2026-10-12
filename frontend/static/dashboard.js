const $ = (id) => document.getElementById(id);

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function card(num, lbl) {
  const d = document.createElement("div");
  d.className = "card";
  d.innerHTML = `<div class="num">${num}</div><div class="lbl">${lbl}</div>`;
  return d;
}

function bars(el, title, data, color) {
  el.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = title;
  h.style.fontSize = ".95rem";
  h.style.margin = "0 0 8px";
  el.appendChild(h);
  const entries = Object.entries(data || {});
  const max = Math.max(1, ...entries.map(([, v]) => v));
  for (const [k, v] of entries) {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `<span>${k}</span><div class="bar-track"><div class="bar-fill" style="width:${(100 * v) / max}%"></div></div><span>${v}</span>`;
    if (color) row.querySelector(".bar-fill").style.background = color;
    el.appendChild(row);
  }
}

function renderAnalytics(a) {
  const c = $("cards");
  c.innerHTML = "";
  c.append(
    card(a.total_sessions, "Sesiones totales"),
    card(a.active_sessions, "En curso"),
    card(a.qualified_count + " (" + a.qualified_rate_pct + "%)", "Pre-calificados"),
    card(a.abandoned_count, "Abandonados"),
    card(a.avg_duration_seconds + "s", "Duración media"),
    card(a.avg_candidate_turns, "Turnos medios"),
    card(a.reengagement.nudged_sessions + " → " + a.reengagement.recovered_sessions, "Re-engagement (recuperados)"),
    card(Object.entries(a.languages || {}).map(([k, v]) => `${k}:${v}`).join(" "), "Idiomas"),
  );
  bars($("funnel"), "Embudo por etapa", a.funnel);
  bars($("dropoffs"), "Abandono por etapa", a.dropoff_by_stage, "var(--warn)");
}

const STATUS_TAG = { qualified: "qualified", disqualified: "disqualified", abandoned: "abandoned", active: "" };

function fmtDur(s) {
  if (s == null) return "—";
  return s >= 60 ? Math.floor(s / 60) + "m " + (s % 60) + "s" : s + "s";
}

async function loadSessions() {
  const params = new URLSearchParams();
  if ($("f-status").value) params.set("status", $("f-status").value);
  if ($("f-qualified").value) params.set("qualified", $("f-qualified").value);
  if ($("f-q").value.trim()) params.set("q", $("f-q").value.trim());
  const { sessions } = await api("/api/sessions?" + params.toString());
  const tb = $("rows");
  tb.innerHTML = "";
  for (const s of sessions) {
    const d = s.data || {};
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const tag = STATUS_TAG[s.status] || "";
    tr.innerHTML = `
      <td>${d.candidate_name || "(sin nombre)"}<br><span class="pill">${s.id.slice(0, 8)}</span></td>
      <td>${d.city || "—"}${d.country ? " · " + d.country : ""}</td>
      <td><span class="tag ${tag}" style="${tag ? "" : "background:var(--panel-2);color:var(--muted)"}">${s.status}</span></td>
      <td>${s.candidate_turns}</td>
      <td>${fmtDur(s.duration_seconds)}</td>
      <td>${s.reengage_attempts || 0}</td>
      <td>${(s.languages || []).join(", ")}</td>
      <td>${(s.last_ts || "").replace("T", " ").slice(0, 16)}</td>`;
    tr.addEventListener("click", () => openDetail(s.id));
    tb.appendChild(tr);
  }
  if (!sessions.length) tb.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">Sin resultados</td></tr>';
}

async function openDetail(id) {
  const s = await api("/api/sessions/" + id);
  const el = $("detail");
  el.innerHTML = "";
  const box = document.createElement("div");
  box.className = "detail";

  const d = s.data || {};
  const h = document.createElement("h2");
  h.textContent = (d.candidate_name || "Sesión " + s.id) + " — " + s.status;
  box.appendChild(h);
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = `${s.id} · ${s.source} · ${s.language} · ${s.started_at}`;
  box.appendChild(sub);

  for (const flag of s.flags || []) {
    const p = document.createElement("span");
    p.className = "pill flag";
    p.textContent = flag;
    box.appendChild(p);
  }
  for (const note of s.notes || []) {
    const p = document.createElement("span");
    p.className = "pill";
    p.textContent = note;
    box.appendChild(p);
  }

  const tr = document.createElement("div");
  tr.className = "transcript";
  for (const m of s.messages) {
    const b = document.createElement("div");
    b.className = "bubble " + (m.role === "user" ? "user" : m.delivered === 0 ? "agent nudge" : "agent");
    b.textContent = m.content;
    tr.appendChild(b);
  }
  box.appendChild(tr);

  const pre = document.createElement("pre");
  pre.className = "json";
  pre.textContent = JSON.stringify({ data: s.data, summary: s.summary, events: s.events }, null, 2);
  box.appendChild(pre);

  el.appendChild(box);
  box.scrollIntoView({ behavior: "smooth" });
}

async function refresh() {
  const [a] = await Promise.all([api("/api/analytics"), loadSessions()]);
  renderAnalytics(a);
}

$("refresh").addEventListener("click", refresh);
$("f-status").addEventListener("change", loadSessions);
$("f-qualified").addEventListener("change", loadSessions);
$("f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSessions(); });

refresh();
setInterval(refresh, 30000);
