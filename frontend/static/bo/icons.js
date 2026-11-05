// Minimal stroke icon set (24×24, currentColor). One consistent visual language
// for the whole backoffice instead of emoji / unicode glyphs.

const PATHS = {
  board: "M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v7h-4z",
  users: "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 4.15a3.5 3.5 0 0 1 0 6.7",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  plus: "M12 5v14M5 12h14",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12.5l4.5 4.5L19 7",
  chevronDown: "M6 9l6 6 6-6",
  chevronRight: "M9 6l6 6-6 6",
  chevronLeft: "M15 6l-6 6 6 6",
  arrowLeft: "M19 12H5M11 18l-6-6 6-6",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  pencil: "M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  link: "M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1",
  calendar: "M5 6h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1zM4 10h16M8 4v4M16 4v4",
  star: "M12 4l2.5 5 5.5.8-4 3.9.9 5.5L12 16.6 7.1 19.2l.9-5.5-4-3.9 5.5-.8z",
  ban: "M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM6.5 6.5l11 11",
  refresh: "M20 11a8 8 0 0 0-14.9-3.5M4 5v4h4M4 13a8 8 0 0 0 14.9 3.5M20 19v-4h-4",
  message: "M5 18l-1 3 4-2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v11z",
  send: "M4 12l16-8-6 16-2.5-6.5z",
  pause: "M8 5v14M16 5v14",
  play: "M7 5l12 7-12 7z",
  filter: "M4 5h16l-6 8v5l-4 2v-7z",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  grip: "M9 6h.01M9 12h.01M9 18h.01M15 6h.01M15 12h.01M15 18h.01",
  arrowUp: "M12 19V5M6 11l6-6 6 6",
  arrowDown: "M12 5v14M6 13l6 6 6-6",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  alert: "M12 4l9 16H3zM12 10v4M12 17h.01",
  shield: "M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6zM9 12l2 2 4-4",
  clock: "M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM12 8v4l3 2",
  pin: "M12 21s-6-5.5-6-11a6 6 0 1 1 12 0c0 5.5-6 11-6 11zM12 12a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  phone: "M5 4h3l2 5-2.5 1.5a11 11 0 0 0 6 6L15 14l5 2v3a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z",
  userPlus: "M15 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 3 18.5V20M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19 8v6M16 11h6",
  flask: "M9 3h6M10 3v6L4.5 18.5A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.5-2.5L14 9V3M7 15h10",
  history: "M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4M12 8v4l3 2",
  book: "M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h10",
  map: "M9 5l-5 2v13l5-2 6 2 5-2V5l-5 2zM9 5v13M15 7v13",
  list: "M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01",
  rules: "M4 6h9M4 12h6M4 18h9M16 9l2 2 4-4M16 17l2 2 4-4",
  trophy: "M8 4h8v5a4 4 0 0 1-8 0zM8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M8 20h8M10 17h4",
  copy: "M9 9h10v11H9zM5 15H4V4h11v1",
  power: "M12 3v8M6.3 7.3a8 8 0 1 0 11.4 0",
  info: "M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM12 11v5M12 8h.01",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z",
  undo: "M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3",
  sparkles: "M12 4l1.5 4.5L18 10l-4.5 1.5L12 16l-1.5-4.5L6 10l4.5-1.5zM19 15l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7z",
  user: "M18 20v-1a4 4 0 0 0-4-4h-4a4 4 0 0 0-4 4v1M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  note: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h6M9 17h4",
  lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3",
  bolt: "M13 3L5 14h6l-1 7 8-11h-6z",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
};

export function icon(name, size = 16, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon ${cls}`.trim());
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", PATHS[name] || PATHS.info);
  svg.appendChild(path);
  return svg;
}
