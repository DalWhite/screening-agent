// Recruiting processes: one row per process with its state and a single menu of
// actions; "Editar" opens the editor.

import { api } from "../api.js";
import { button, chip, clear, confirmDialog, emptyState, h, icon, iconButton, linkButton, loc, menu, modal, relTime, toast } from "../kit.js";
import { loadMeta } from "../shared.js";

export async function renderProcesses(root) {
  const { profiles } = await api.get("/api/bo/profiles");

  async function refresh() {
    await loadMeta(true);
    window.dispatchEvent(new Event("bo:meta-changed"));
    renderProcesses(root);
  }

  function item(p) {
    const status = !p.published ? chip("Borrador sin publicar", "warn")
      : p.active ? chip("Activo", "ok") : chip("Inactivo", "muted");
    const editHref = `#/processes/${encodeURIComponent(p.id)}`;
    const more = iconButton("more", `Más acciones de ${loc(p.role) || p.id}`, () => menu(more, [
      { label: "Duplicar", icon: "copy", onClick: () => createModal({ title: `Duplicar «${loc(p.role)}»`, from: p }) },
      p.published ? { label: p.active ? "Desactivar" : "Activar", icon: "power", onClick: async () => {
        await api.post(`/api/bo/profiles/${p.id}/active`, { active: !p.active });
        toast(p.active ? "Proceso desactivado" : "Proceso activado");
        refresh();
      } } : null,
      p.published && p.active ? { label: "Abrir chat de candidato", icon: "external", onClick: () => window.open(`/?job=${encodeURIComponent(p.id)}`, "_blank", "noopener") } : null,
      "separator",
      { label: p.sessions ? "Eliminar (tiene candidatos)" : "Eliminar", icon: "trash", danger: !p.sessions, disabled: !!p.sessions,
        onClick: () => remove(p) },
    ], { align: "end" }));
    return h("article", { class: "process-item", testid: `process-${p.id}` },
      h("div", { class: "process-icon" }, icon("sliders", 18)),
      h("div", { class: "grow" },
        h("div", { class: "row" }, h("a", { href: editHref, class: "title" }, loc(p.role) || p.id), status,
          p.has_draft && p.published ? chip("Cambios sin publicar", "info") : null),
        h("div", { class: "sub" }, h("span", {}, p.company || "—"), h("span", {}, `Asistente ${p.agent_name || "—"}`),
          h("span", {}, p.published ? `Versión r${p.latest_revision}` : "Sin publicar"), h("span", {}, `Actualizado ${relTime(p.updated_at)}`))),
      h("div", { class: "process-stats" },
        h("div", {}, h("strong", {}, String(p.questions)), "preguntas"),
        h("div", {}, h("strong", {}, String(p.rules)), "reglas"),
        h("div", {}, h("strong", {}, String(p.sessions)), "candidatos")),
      h("div", { class: "row" }, linkButton("Editar", editHref, { icon: "pencil" }), more));
  }

  function createModal({ title, from } = {}) {
    const form = h("form", { class: "form-grid", id: "process-form" },
      h("label", { class: "field" }, h("span", {}, "Puesto"),
        h("input", { name: "role", required: !from, value: from ? `${loc(from.role)} (copia)` : "", placeholder: "Mozo/a de almacén", "aria-label": "Puesto" })),
      h("label", { class: "field" }, h("span", {}, "Empresa"), h("input", { name: "company", required: !from, value: from?.company || "", "aria-label": "Empresa" })),
      h("label", { class: "field span-2" }, h("span", {}, "Identificador"),
        h("input", { name: "id", required: true, pattern: "[a-z][a-z0-9_]{2,40}", placeholder: "mozo_almacen",
                     "aria-label": "Identificador (no se puede cambiar)", value: from ? `${from.id}_copia` : "" }),
        h("span", { class: "hint" }, "Se usa en los enlaces de candidato (/?job=…). Minúsculas, números y _; no se puede cambiar.")));
    const role = form.querySelector('[name="role"]');
    const idInput = form.querySelector('[name="id"]');
    let idTouched = !!from;
    idInput.addEventListener("input", () => { idTouched = true; });
    role.addEventListener("input", () => {
      if (idTouched) return;
      idInput.value = role.value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
    });
    const submit = button("Crear borrador", { variant: "primary", type: "submit", form: "process-form" });
    const { close } = modal(title, form, [submit], {
      description: from ? `Se copian las preguntas, reglas y puntuación de «${loc(from.role)}».` : "Empieza con nombre y fecha de inicio; añade el resto en el editor.",
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      try {
        await api.post("/api/bo/profiles", { ...data, from_profile: from?.id, role: data.role || undefined });
        close();
        toast("Borrador creado: publícalo cuando esté listo");
        location.hash = `#/processes/${encodeURIComponent(data.id)}`;
      } catch (err) { toast(err.message, "error"); }
    });
  }

  async function remove(p) {
    if (!(await confirmDialog("Eliminar proceso", `¿Eliminar «${loc(p.role) || p.id}» y todo su historial de versiones?`,
      { confirmLabel: "Eliminar definitivamente", danger: true }))) return;
    try {
      await api.del(`/api/bo/profiles/${p.id}`);
      toast("Proceso eliminado");
      refresh();
    } catch (err) { toast(err.message, "error"); }
  }

  clear(root,
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Procesos de selección"),
        h("p", { class: "muted" }, "Qué pregunta el asistente, qué reglas aplica y cómo puntúa a los candidatos.")),
      button("Nuevo proceso", { icon: "plus", variant: "primary", onClick: () => createModal({ title: "Nuevo proceso" }) })),
    profiles.length ? h("div", { class: "process-list", testid: "processes-table" }, profiles.map(item))
      : emptyState("Todavía no hay procesos.", "sliders"));
  return () => {};
}
