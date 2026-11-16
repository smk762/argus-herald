// Dashboard crosshair: one vertical line across every panel in a section, snapped to the nearest
// day, with a tooltip listing every series on that day. Keyboard: focus a chart, use the arrows.
// Values come from data attributes and are inserted with textContent only.
const W = 760, LEFT = 52, RIGHT = 14;

function setup(container) {
  const charts = Array.from(container.querySelectorAll("svg.chart[data-values]"));
  const tip = container.querySelector(".tooltip");
  if (!charts.length || !tip) return;
  const series = charts.map((svg) => ({
    svg,
    label: svg.dataset.label || "",
    values: JSON.parse(svg.dataset.values || "[]"),
    line: svg.querySelector(".crosshair"),
  }));
  const n = series[0].values.length;
  const start = new Date(`${charts[0].dataset.start}T00:00:00Z`);
  let index = null;

  const xOf = (i) => LEFT + (n > 1 ? ((W - LEFT - RIGHT) * i) / (n - 1) : (W - LEFT - RIGHT) / 2);
  const format = (v) => (v === null || v === undefined ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }));

  function show(i, anchor) {
    if (n === 0) return;
    index = Math.max(0, Math.min(n - 1, i));
    const x = xOf(index);
    for (const s of series) {
      if (!s.line) continue;
      s.line.setAttribute("x1", String(x));
      s.line.setAttribute("x2", String(x));
      s.line.setAttribute("visibility", "visible");
    }
    const day = new Date(start.getTime() + index * 86400000);
    tip.replaceChildren();
    const head = document.createElement("div");
    head.className = "tip-date";
    head.textContent = day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    tip.appendChild(head);
    for (const s of series) {
      const row = document.createElement("div");
      row.className = "tip-row";
      const name = document.createElement("span");
      name.textContent = s.label;
      const value = document.createElement("strong");
      value.textContent = format(s.values[index]);
      row.append(value, name);
      tip.appendChild(row);
    }
    tip.hidden = false;
    const box = container.getBoundingClientRect();
    const svgBox = anchor.getBoundingClientRect();
    const px = svgBox.left - box.left + (x / W) * svgBox.width;
    const left = px + 14 + tip.offsetWidth > box.width ? px - 14 - tip.offsetWidth : px + 14;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${svgBox.top - box.top + 8}px`;
  }

  function hide() {
    index = null;
    tip.hidden = true;
    for (const s of series) s.line && s.line.setAttribute("visibility", "hidden");
  }

  for (const s of series) {
    s.svg.setAttribute("tabindex", "0");
    s.svg.addEventListener("pointermove", (event) => {
      const box = s.svg.getBoundingClientRect();
      if (!box.width) return;
      const x = ((event.clientX - box.left) / box.width) * W;
      const step = n > 1 ? (W - LEFT - RIGHT) / (n - 1) : 1;
      show(Math.round((x - LEFT) / step), s.svg);
    });
    s.svg.addEventListener("pointerleave", hide);
    s.svg.addEventListener("blur", hide);
    s.svg.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const move = event.key === "ArrowLeft" ? -1 : 1;
        show(index === null ? n - 1 : index + move, s.svg);
      } else if (event.key === "Escape") {
        hide();
      }
    });
  }
}

document.querySelectorAll("[data-crosshair]").forEach(setup);
