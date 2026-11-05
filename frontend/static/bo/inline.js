// Inline-editable data tables: every cell is an input, edits apply in place.
// Structural actions (add / delete / reorder / expand details) are row buttons.
//
// Column spec:
//   { label, key?, get?(row), set?(row, value), type: text|textarea|number|select|checkbox|tags|custom,
//     options?: [[value, label]] | (row) => [[value, label]], placeholder?, width?, render?(row, i, ctx) }
// A cell input carries data-path="<section>.<index>.<key>" so validation problems can be pinned to it.

import { button, h, iconButton } from "./kit.js";

export function fieldInput(type, value, { options = [], placeholder = "", label = "", path = "", onChange, rows = 2 } = {}) {
  let el;
  if (type === "select") {
    el = h("select", { "aria-label": label },
      options.map(([v, l]) => h("option", { value: String(v), selected: String(v) === String(value ?? "") }, l)));
    if (value !== undefined && value !== null && !options.some(([v]) => String(v) === String(value))) {
      el.prepend(h("option", { value: String(value), selected: true }, `${value} (?)`));
    }
    el.addEventListener("change", () => onChange?.(el.value));
  } else if (type === "checkbox") {
    el = h("input", { type: "checkbox", checked: !!value, "aria-label": label });
    el.addEventListener("change", () => onChange?.(el.checked));
  } else if (type === "textarea") {
    const text = String(value ?? "");
    // size to the content (roughly 42 characters per line in a table cell), up to 6 lines
    const fit = Math.min(6, Math.max(rows, Math.ceil(text.length / 42), text.split("\n").length));
    el = h("textarea", { rows: fit, placeholder, "aria-label": label });
    el.value = text;
    el.addEventListener("change", () => onChange?.(el.value));
  } else if (type === "number") {
    el = h("input", { type: "number", value: value ?? "", placeholder, "aria-label": label, step: "any" });
    el.addEventListener("change", () => onChange?.(el.value === "" ? null : Number(el.value)));
  } else if (type === "tags") {
    el = h("input", { value: (value || []).join(", "), placeholder, "aria-label": label });
    el.addEventListener("change", () => onChange?.(el.value.split(",").map((t) => t.trim()).filter(Boolean)));
  } else {
    el = h("input", { value: value ?? "", placeholder, "aria-label": label });
    el.addEventListener("change", () => onChange?.(el.value));
  }
  el.classList.add("cell-input");
  if (path) el.dataset.path = path;
  return el;
}

export function inlineTable({
  section, rows, columns, onChange, onStructure, newRow, addLabel = "Añadir fila", details, movable = false,
  emptyText = "Sin filas todavía.", rowLabel = (row, i) => `fila ${i + 1}`, testid, openRows,
}) {
  const open = openRows || new Set();  // caller-owned so expanded rows survive re-renders
  const table = h("table", { class: "inline-table", testid: testid || `table-${section}` });
  const body = h("tbody");

  function cell(col, row, i) {
    const path = `${section}.${i}.${col.key || col.pathKey || ""}`;
    if (col.render) return col.render(row, i, { path, onChange, onStructure });
    const value = col.get ? col.get(row) : row[col.key];
    const options = typeof col.options === "function" ? col.options(row) : col.options;
    return fieldInput(col.type || "text", value, {
      options, placeholder: col.placeholder || "", path, rows: col.rows,
      label: `${col.label} (${rowLabel(row, i)})`,
      onChange: (v) => {
        if (col.set) col.set(row, v); else row[col.key] = v;
        if (col.structural) onStructure(); else onChange();
      },
    });
  }

  function draw() {
    body.replaceChildren();
    if (!rows.length) {
      body.appendChild(h("tr", {}, h("td", { colspan: columns.length + 2, class: "muted empty-row" }, emptyText)));
    }
    rows.forEach((row, i) => {
      const actions = h("td", { class: "row-actions" },
        movable ? iconButton("arrowUp", `Subir ${rowLabel(row, i)}`, () => { rows.splice(i - 1, 0, rows.splice(i, 1)[0]); onStructure(); }, { disabled: i === 0 }) : null,
        movable ? iconButton("arrowDown", `Bajar ${rowLabel(row, i)}`, () => { rows.splice(i + 1, 0, rows.splice(i, 1)[0]); onStructure(); }, { disabled: i === rows.length - 1 }) : null,
        details ? iconButton(open.has(row) ? "chevronDown" : "chevronRight", `Detalles de ${rowLabel(row, i)}`,
          () => { open.has(row) ? open.delete(row) : open.add(row); draw(); }, { "aria-expanded": String(open.has(row)) }) : null,
        iconButton("trash", `Eliminar ${rowLabel(row, i)}`, () => { rows.splice(i, 1); open.delete(row); onStructure(); }));
      body.appendChild(h("tr", { dataset: { index: i } }, columns.map((col) => h("td", { class: col.class || "" }, cell(col, row, i))), actions));
      if (details && open.has(row)) {
        body.appendChild(h("tr", { class: "details-row" }, h("td", { colspan: columns.length + 1 }, details(row, i))));
      }
    });
  }

  table.append(
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { style: c.width ? { width: c.width } : {} }, c.label)), h("th", { class: "row-actions" }, ""))),
    body);
  draw();
  const add = newRow ? button(addLabel, { icon: "plus", variant: "ghost", class: "btn btn-ghost add-row",
    onClick: () => { const row = newRow(); rows.push(row); if (details) open.add(row); onStructure(); } }) : null;
  return h("div", { class: "inline-wrap" }, h("div", { class: "table-wrap" }, table), add);
}

// Key/value table: one row per property of a single object (general settings, messages).
export function propertyTable({ rows, onChange, testid, headers }) {
  const width = Math.max(...rows.map((r) => (r.cells || [r]).length));
  return h("div", { class: "table-wrap" }, h("table", { class: "inline-table props", testid },
    headers ? h("thead", {}, h("tr", {}, headers.map((t) => h("th", {}, t)))) : null,
    h("tbody", {}, rows.map((r) => {
      const cells = r.cells || [r];
      return h("tr", {},
        h("th", { scope: "row" }, r.label, r.hint ? h("div", { class: "muted small" }, r.hint) : null),
        ...cells.map((c, i) => h("td", { colspan: i === cells.length - 1 && cells.length < width ? width - cells.length + 1 : null },
          fieldInput(c.type || "text", c.get(), {
            options: c.options, path: c.path, label: c.aria || r.label, rows: c.rows,
            onChange: (v) => { c.set(v); onChange(); },
          }))));
    }))));
}
