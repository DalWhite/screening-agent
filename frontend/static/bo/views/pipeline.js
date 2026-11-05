// Kanban board. Cards move by drag & drop (or their menu) and open the quick
// view on click. Stages are edited in place: rename (double-click the title),
// colour, reorder, add and delete from each column's menu.

import { api, qs } from "../api.js";
import {
  avatar, button, clear, confirmDialog, debounce, dot, emptyState, h, icon, iconButton, loc, menu, modal, popover,
  relTimeShort, scoreBadge, toast,
} from "../kit.js";
import {
  candidateHref, flagChips, loadMeta, moveCandidate, navigate, pipelineLabel, pipelineStages, profileChip,
  questionLabel, store,
} from "../shared.js";
import { openQuickView } from "./quickview.js";

export async function renderPipeline(root, params) {
  let meta = await loadMeta();
  const state = { q: params.get("q") || "", assignee: params.get("assignee") || "" };
  let total = 0;

  const search = h("input", { type: "search", placeholder: "Buscar en el pipeline…", value: state.q, "aria-label": "Buscar en el pipeline" });
  const assignee = h("select", { "aria-label": "Responsable" });
  const fillAssignees = () => clear(assignee,
    h("option", { value: "" }, "Todos los responsables"),
    h("option", { value: "__none__", selected: state.assignee === "__none__" }, "Sin asignar"),
    (meta.assignees || []).map((a) => h("option", { value: a, selected: a === state.assignee }, a)));
  fillAssignees();
  const subtitle = h("p", { class: "muted" });
  const board = h("div", { class: "board", testid: "board" });

  clear(root,
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Pipeline"), subtitle),
      h("div", { class: "toolbar" }, h("div", { class: "input-icon" }, icon("search", 15), search), assignee,
        iconButton("refresh", "Actualizar", () => load()))),
    board);

  const updateUrl = () => history.replaceState(null, "", `#/pipeline${qs(state) ? "?" + qs(state) : ""}`);
  search.addEventListener("input", debounce(() => { state.q = search.value.trim(); updateUrl(); load(); }, 250));
  assignee.addEventListener("change", () => { state.assignee = assignee.value; updateUrl(); load(); });

  let seq = 0;
  async function load() {
    const mine = ++seq;
    const data = await api.get(`/api/bo/pipeline?${qs({ ...state, profile_id: store.profile })}`);
    if (mine !== seq || board.querySelector(".dragging") || board.querySelector(".column input")) return;
    total = data.columns.reduce((n, c) => n + c.count, 0);
    subtitle.textContent = `${total} candidato${total === 1 ? "" : "s"} · arrastra las tarjetas para cambiarlas de etapa`;
    clear(board, data.columns.map(column), addColumn());
  }

  async function stagesChanged() {
    meta = await loadMeta(true);
    await load();
  }

  // ---------- stage editing ----------

  function renameInline(header, col) {
    const title = header.querySelector("h2");
    const input = h("input", { value: loc(col.label), "aria-label": `Nombre de la etapa ${loc(col.label)}`, maxlength: 40 });
    title.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const label = input.value.trim();
      input.replaceWith(title);  // leave edit mode first: the board never reloads under an open input
      if (save && label && label !== loc(col.label)) {
        title.textContent = label;
        try { await api.patch(`/api/bo/stages/${col.id}`, { label }); toast("Etapa renombrada"); } catch (err) { toast(err.message, "error"); }
      }
      await stagesChanged();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }

  function colorPicker(anchor, col) {
    popover(anchor, h("div", {}, h("div", { class: "menu-header" }, "Color"),
      h("div", { class: "swatches" }, (meta.stage_colors || []).map((c) => h("button", {
        type: "button", class: `swatch ${c === col.color ? "on" : ""}`, style: { background: `var(--c-${c})` }, "aria-label": `Color ${c}`,
        onclick: async () => { await api.patch(`/api/bo/stages/${col.id}`, { color: c }); await stagesChanged(); },
      })))));
  }

  async function moveStage(col, delta) {
    const ids = pipelineStages().map((s) => s.id);
    const i = ids.indexOf(col.id);
    const j = i + delta;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api.put("/api/bo/stages/order", { ids });
    await stagesChanged();
  }

  async function deleteStage(col) {
    if (!col.count) {
      if (!(await confirmDialog(`Eliminar «${loc(col.label)}»`, "La etapa está vacía. ¿Eliminarla del pipeline?", { confirmLabel: "Eliminar", danger: true }))) return;
      await api.del(`/api/bo/stages/${col.id}`);
    } else {
      const target = h("select", { "aria-label": "Mover sus candidatos a" },
        pipelineStages().filter((s) => s.id !== col.id).map((s) => h("option", { value: s.id }, loc(s.label))));
      await new Promise((resolve) => {
        const ok = button("Eliminar y mover", { variant: "danger", onClick: async () => {
          try {
            await api.del(`/api/bo/stages/${col.id}?move_to=${encodeURIComponent(target.value)}`);
            close();
            resolve();
          } catch (err) { toast(err.message, "error"); }
        } });
        const { close } = modal(`Eliminar «${loc(col.label)}»`,
          h("label", { class: "field" }, h("span", {}, `Tiene ${col.count} candidato(s). ¿A qué etapa los movemos?`), target), [ok]);
      });
    }
    toast("Etapa eliminada");
    await stagesChanged();
  }

  function stageMenu(anchor, col, header) {
    const stages = pipelineStages();
    const i = stages.findIndex((s) => s.id === col.id);
    menu(anchor, [
      { label: "Renombrar", icon: "pencil", onClick: () => renameInline(header, col) },
      { label: "Cambiar color", icon: "sparkles", onClick: () => colorPicker(anchor, col) },
      { label: "Mover a la izquierda", icon: "chevronLeft", disabled: i <= 0, onClick: () => moveStage(col, -1) },
      { label: "Mover a la derecha", icon: "chevronRight", disabled: i >= stages.length - 1, onClick: () => moveStage(col, 1) },
      { label: "Añadir etapa después", icon: "plus", onClick: () => addInline(col.id) },
      "separator",
      { label: col.system ? "Etapa del sistema (no se puede eliminar)" : "Eliminar etapa", icon: "trash", danger: !col.system,
        disabled: col.system, onClick: () => deleteStage(col) },
    ], { align: "end" });
  }

  function addInline(after = null) {
    const input = h("input", { placeholder: "Nombre de la etapa", "aria-label": "Nombre de la nueva etapa", maxlength: 40 });
    const slot = h("section", { class: "column add-column" }, h("header", {}, input));
    if (after) {
      const afterCol = board.querySelector(`[data-testid="column-${after}"]`);
      afterCol.after(slot);
    } else {
      board.querySelector(".add-column:last-child")?.replaceWith(slot);
    }
    input.focus();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const label = input.value.trim();
      slot.remove();
      if (save && label) {
        try { await api.post("/api/bo/stages", { label, after }); toast(`Etapa «${label}» creada`); } catch (err) { toast(err.message, "error"); }
      }
      await stagesChanged();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }

  function addColumn() {
    return h("section", { class: "column add-column" },
      button("Nueva etapa", { icon: "plus", variant: "ghost", testid: "add-stage", onClick: () => addInline() }));
  }

  // ---------- columns & cards ----------

  function column(col) {
    const stage = pipelineStages().find((s) => s.id === col.id) || col;
    const info = { ...col, color: stage.color, system: stage.system };
    const title = h("h2", { title: "Doble clic para renombrar" }, loc(col.label));
    const more = iconButton("more", `Opciones de la etapa ${loc(col.label)}`, () => {});
    more.classList.add("col-menu");
    const header = h("header", {}, dot(info.color), title, h("span", { class: "count", testid: "column-count" }, String(col.count)), more);
    more.addEventListener("click", () => stageMenu(more, info, header));
    title.addEventListener("dblclick", () => renameInline(header, info));
    const list = h("div", { class: "col-list" }, col.items.length ? col.items.map(card) : h("p", { class: "col-empty" }, "Suelta aquí"));
    // the board shows the most recent cards of each stage; the full list is one click away
    const hidden = col.count - col.items.length;
    const footer = hidden > 0
      ? h("button", { type: "button", class: "col-more", testid: `column-more-${col.id}`,
        onclick: () => navigate("candidates", { pipeline_stage: col.id, q: state.q, assignee: state.assignee }) },
        `Ver los ${col.count} en la lista`, icon("chevronRight", 14))
      : null;
    const el = h("section", { class: "column", testid: `column-${col.id}`, "aria-label": loc(col.label) }, header, list, footer);
    el.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("text/candidate")) { e.preventDefault(); el.classList.add("drop"); } });
    el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("drop"); });
    el.addEventListener("drop", async (e) => {
      e.preventDefault();
      el.classList.remove("drop");
      const id = e.dataTransfer.getData("text/candidate");
      const from = e.dataTransfer.getData("text/from");
      if (!id || from === col.id) return;
      try { await moveCandidate(id, col.id); } catch (err) { toast(err.message, "error"); }
      load();
    });
    return el;
  }

  function card(c) {
    const more = iconButton("more", `Acciones de ${c.name || "candidato"}`, () => {});
    more.classList.add("card-more");
    more.addEventListener("click", (e) => {
      e.stopPropagation();
      menu(more, [
        { label: "Vista rápida", icon: "eye", onClick: () => openQuickView(c.id) },
        { label: "Abrir ficha", icon: "external", onClick: () => navigate(`candidates/${c.id}`) },
        "separator", { header: "Mover a" },
        ...pipelineStages().filter((s) => s.id !== c.pipeline_stage).map((s) => ({
          label: loc(s.label), tone: s.color, testid: `move-${s.id}`,
          onClick: async () => { await moveCandidate(c.id, s.id); load(); },
        })),
      ], { align: "end" });
    });
    const el = h("article", { class: "card", draggable: "true", testid: "candidate-card", dataset: { id: c.id }, tabindex: "0",
                               "aria-label": `${c.name || "Sin nombre"}, ${pipelineLabel(c.pipeline_stage)}` },
      h("div", { class: "card-top" }, h("a", { class: "card-name", href: candidateHref(c.id), onclick: (e) => { e.preventDefault(); openQuickView(c.id); } }, c.name || "Sin nombre"), scoreBadge(c.score)),
      h("div", { class: "card-meta" }, profileChip(c.profile_id, true), c.city ? h("span", { class: "meta" }, icon("pin", 13), c.city) : null),
      c.status === "active" || c.status === "verifying" ? h("div", { class: "card-line" },
        c.bot_paused ? "Atendido por reclutador" : c.status === "verifying" ? "Verificando datos…" : c.stage === "intro" ? "Aún no ha empezado el chat" : `En conversación · ${questionLabel(c.profile_id, c.stage)}`) : null,
      c.awaiting_reply ? h("div", { class: "card-line" }, h("span", { class: "badge-reply" }, icon("message", 12), "Esperando tu respuesta")) : null,
      (c.flags || []).length ? h("div", { class: "card-flags" }, flagChips(c.flags, 2)) : null,
      h("footer", {},
        c.assignee ? h("span", { title: `Responsable: ${c.assignee}` }, avatar(c.assignee)) : null,
        h("span", { title: `Última actividad: ${c.last_ts}` }, relTimeShort(c.last_ts)), more));
    el.addEventListener("click", (e) => { if (!e.target.closest("button")) openQuickView(c.id); });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") openQuickView(c.id); });
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/candidate", c.id);
      e.dataTransfer.setData("text/from", c.pipeline_stage);
      e.dataTransfer.effectAllowed = "move";
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
    return el;
  }

  try {
    await load();
  } catch (err) {
    clear(board, emptyState(err.message, "alert"));
  }
  const onChange = () => load().catch(() => {});
  window.addEventListener("bo:candidate-changed", onChange);
  const timer = setInterval(() => { if (!document.hidden && !document.querySelector(".dragging, .drawer")) onChange(); }, 20000);
  return () => { clearInterval(timer); window.removeEventListener("bo:candidate-changed", onChange); };
}
