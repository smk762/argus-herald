// Properties of the review UI: the only guard between a draft and a public post.
// Run against tests/bombadil/serve.py (seeded store, fake publishers); see run.sh.
import { always, eventually, now } from "@antithesishq/bombadil";
import { actions, extract, registerCustomAction, weighted } from "@antithesishq/bombadil/browser";
import { back, inputs, reload, scroll } from "@antithesishq/bombadil/browser/defaults/actions";
import { PATHS, forgePost, goto, localClicks } from "./lib.ts";

export { noConsoleErrors, noServerErrors, noUncaughtExceptions, noUnhandledPromiseRejections } from "./lib.ts";

const DENY_TERM = "zebra-canyon-7"; // the fixture deny-list's only term (serve.py)

type Page = {
  path: string;
  draft: number | null;
  status: string | null;
  blocked: boolean;
  approveShown: boolean;
  approveEnabled: boolean;
  publishShown: boolean;
  publishEnabled: boolean;
  scheduleShown: boolean;
  openInX: boolean;
  editable: boolean;
  bodyHasTerm: boolean;
  acceptable: boolean;
  flashBad: boolean;
  afterSave: number | null;
};

const page = extract((state): Page => {
  const doc = state.document;
  const path = state.window.location.pathname;
  const match = /^\/drafts\/(\d+)$/.exec(path);
  const draft = match ? Number(match[1]) : null;
  const badge = doc.querySelector(".sheet-head .status");
  const status = badge
    ? (Array.from(badge.classList).find((c) => c.startsWith("status-")) ?? "").slice("status-".length)
    : null;
  const buttons = Array.from(doc.querySelectorAll("button"));
  const named = (label: string) => buttons.filter((b) => (b.textContent ?? "").trim() === label);
  const body = doc.querySelector<HTMLTextAreaElement>("form.editor textarea#body");
  const last = state.lastAction;
  let afterSave: number | null = null;
  if (last && typeof last === "object") {
    const clickedSave =
      "Click" in last &&
      ((last.Click.fingerprint.textContent ?? "").trim() === "Save" || last.Click.fingerprint.accessibleName === "Save");
    const customSave = "Custom" in last && last.Custom.name === "editAndSave";
    if ((clickedSave || customSave) && draft !== null) afterSave = draft;
  }
  return {
    path,
    draft,
    status,
    // An unresolved privacy hit, an unavailable gate, or an attachment problem (missing alt text).
    blocked: doc.querySelector("section.privacy-bad") !== null || doc.querySelector(".media .flash-bad") !== null,
    approveShown: named("Approve").length > 0,
    approveEnabled: named("Approve").some((b) => !b.disabled),
    publishShown: named("Publish").length > 0,
    publishEnabled: named("Publish").some((b) => !b.disabled),
    scheduleShown: doc.querySelector("section.schedule") !== null,
    openInX: Array.from(doc.querySelectorAll("a")).some((a) => (a.textContent ?? "").trim() === "Open in X"),
    editable: body !== null && !body.readOnly && named("Save").length > 0,
    bodyHasTerm: body !== null && body.defaultValue.includes(DENY_TERM),
    acceptable: doc.querySelector("form.accept-form") !== null,
    flashBad: doc.querySelector("body > .flash-bad") !== null,
    afterSave,
  };
});

// ---- properties ----------------------------------------------------------------------------

export const approveDisabledWhileBlocked = always(
  () => !(page.current.blocked && (page.current.approveEnabled || page.current.publishEnabled)),
);

// Even when Approve or Publish is forced past a disabled button, the server keeps it in review.
export const approvedDraftsAreClean = always(
  () => !(["approved", "publishing", "handed_off"].includes(page.current.status ?? "") && page.current.blocked),
);

// Publishing is for approved drafts (and retrying a failed or interrupted publish).
export const publishOnlyWhenApproved = always(
  () => !page.current.publishShown || ["approved", "failed", "publishing"].includes(page.current.status ?? ""),
);

export const scheduleOnlyWhenApproved = always(() => !page.current.scheduleShown || page.current.status === "approved");

// "Open in X" appears once an approved X draft has been handed to a person to post.
export const openInXOnlyWhenHandedOff = always(() => !page.current.openInX || page.current.status === "handed_off");

export const saveReturnsToPending = always(() => {
  const saved = page.current.afterSave;
  if (saved === null) return now(() => true);
  return eventually(
    () => page.current.draft === saved && (page.current.status === "pending" || page.current.flashBad),
  ).within(5, "seconds");
});

// ---- themes --------------------------------------------------------------------------------

type Contrast = { theme: string | null; rootLuminance: number; worst: { text: string; ratio: number }[] };

const contrast = extract((state): Contrast => {
  const win = state.window;
  const doc = state.document;
  const parse = (value: string): number[] | null => {
    const m = /rgba?\(([^)]+)\)/.exec(value);
    if (!m) return null;
    const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return parts.length >= 3 ? [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1] : null;
  };
  const luminance = ([r, g, b]: number[]) => {
    const lin = (c: number) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  };
  const CANVAS = [255, 255, 255, 1];
  const background = (el: Element | null): number[] => {
    for (let node = el; node; node = node.parentElement) {
      const color = parse(win.getComputedStyle(node).backgroundColor);
      if (color && color[3] > 0.5) return color;
    }
    return CANVAS;
  };
  // An unpainted root computes to rgba(0, 0, 0, 0): what shows is the browser's white canvas, not black.
  const root = parse(win.getComputedStyle(doc.documentElement).backgroundColor);
  // Plain-text answers (404, an expired form) and static files have no theme to check.
  const styled = doc.querySelector('link[href="/static/review.css"]') !== null;
  const cookie = styled ? /(?:^|;\s*)theme=(light|dark)/.exec(doc.cookie) : null;
  const worst: { text: string; ratio: number }[] = [];
  const selector = "main p, main a, main label, main li, main h1, main h2, .status, .badge, .flash, .kind, button:not(:disabled), .top a";
  for (const el of styled ? Array.from(doc.querySelectorAll(selector)).slice(0, 300) : []) {
    const style = win.getComputedStyle(el);
    const text = (el.textContent ?? "").trim();
    if (!text || style.visibility === "hidden" || style.display === "none" || Number(style.opacity) < 1) continue;
    const fg = parse(style.color);
    if (!fg) continue;
    const a = luminance(fg);
    const b = luminance(background(el));
    const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) worst.push({ text: `${el.tagName.toLowerCase()} "${text.slice(0, 40)}"`, ratio });
  }
  return {
    theme: cookie ? cookie[1] : null,
    rootLuminance: luminance(root && root[3] > 0.5 ? root : CANVAS),
    worst: worst.slice(0, 5),
  };
});

// WCAG AA text contrast, in whichever theme is showing.
export const readableContrast = always(() => contrast.current.worst.length === 0);

// The theme picked (the cookie set by the header toggle) is the one on screen.
export const themeMatchesChoice = always(() => {
  const { theme, rootLuminance } = contrast.current;
  return theme === null || (theme === "dark" ? rootLuminance < 0.05 : rootLuminance > 0.5);
});

// ---- actions -------------------------------------------------------------------------------

const editAndSave = registerCustomAction("editAndSave", async (document, _window, text: string, scrub: boolean) => {
  const body = document.querySelector<HTMLTextAreaElement>("form.editor textarea#body");
  const save = Array.from(document.querySelectorAll<HTMLButtonElement>("form.editor button")).find(
    (b) => (b.textContent ?? "").trim() === "Save",
  );
  if (!body || !save) throw new Error("no editable draft on this page");
  body.value = (scrub ? body.value.split(DENY_TERM).join("") : body.value) + text;
  save.click();
});

// Submits the form behind a button even when the button is disabled.
const force = registerCustomAction("force", async (document, _window, label: string) => {
  const button = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
    (b) => (b.textContent ?? "").trim() === label,
  );
  const form = button?.form;
  if (!button || !form) throw new Error(`no ${label} button`);
  const target = button.getAttribute("formaction");
  if (target) form.action = target;
  form.submit();
});

const acceptHit = registerCustomAction("acceptHit", async (document) => {
  const form = document.querySelector<HTMLFormElement>("form.accept-form");
  const note = form?.querySelector<HTMLInputElement>("input[name=note]");
  if (!form || !note) throw new Error("no privacy hit to accept");
  note.value = "Fixture term, safe in this test";
  form.submit();
});

export const reviewActions = actions(() => {
  const p = page.current;
  const out = [];
  if (p.editable) {
    out.push(editAndSave(" Thanks for testing.", false));
    out.push(editAndSave(` Now with ${DENY_TERM}.`, false));
    if (p.bodyHasTerm) out.push(editAndSave("", true));
  }
  if (p.approveShown) out.push(force("Approve"));
  if (p.publishShown) out.push(force("Publish"));
  if (p.acceptable) out.push(acceptHit());
  // Straight to the endpoint without this session's CSRF token, as a script or a stale tab would.
  if (p.draft !== null) out.push(forgePost(`/drafts/${p.draft}/approve`, { csrf: "stale" }));
  return out;
});

export const jump = actions(() => PATHS.map((path) => goto(path)));


// ---- dashboard -----------------------------------------------------------------------------

const dash = extract((state) => {
  const doc = state.document;
  if (state.window.location.pathname !== "/dashboard") return null;
  const broken = Array.from(doc.querySelectorAll("svg.chart path, svg.chart line, svg.chart text")).filter((el) =>
    /NaN|undefined|Infinity/.test(
      (el.getAttribute("d") ?? "") + (el.getAttribute("x1") ?? "") + (el.getAttribute("y1") ?? "") + (el.textContent ?? ""),
    ),
  ).length;
  const tips = Array.from(doc.querySelectorAll<HTMLElement>("[data-crosshair]")).map((box) => {
    const tip = box.querySelector<HTMLElement>(".tooltip");
    return {
      visible: tip !== null && !tip.hidden,
      rows: tip ? tip.querySelectorAll(".tip-row").length : 0,
      charts: box.querySelectorAll("svg.chart[data-values]").length,
      date: tip?.querySelector(".tip-date")?.textContent ?? "",
    };
  });
  const text = doc.body.innerText;
  return { broken, tips, rawMarkup: /<script|&lt;|undefined|NaN/.test(text) };
});

export const dashboardChartsAreWellFormed = always(() => dash.current === null || dash.current.broken === 0);

// One tooltip, every series: a visible tooltip lists one row per chart in its section, and a date.
export const tooltipListsEverySeries = always(
  () => dash.current === null || dash.current.tips.every((t) => !t.visible || (t.rows === t.charts && t.date !== "")),
);

export const dashboardShowsNoRawValues = always(() => dash.current === null || !dash.current.rawMarkup);

// ---- dashboard: clips panel ------------------------------------------------------------------

const FRESHNESS = ["redo", "check", "unknown", "fresh", "not checked"]; // the panel's sort order
const RATINGS = ["strong", "typical", "weak", "no data"];

const clipPanel = extract((state) => {
  const doc = state.document;
  if (state.window.location.pathname !== "/dashboard") return null;
  const rows = Array.from(doc.querySelectorAll<HTMLTableRowElement>("table.clips tbody tr")).map((tr) => ({
    freshness: tr.getAttribute("data-freshness") ?? "",
    best: tr.getAttribute("data-best") ?? "",
    freshBadges: Array.from(tr.querySelectorAll(".fresh-badge")).map((b) => (b.textContent ?? "").trim()),
    perfBadges: Array.from(tr.querySelectorAll(".perf-badge")).map((b) => (b.textContent ?? "").trim()),
    marked: tr.classList.contains("rerecord"),
    markText: (tr.querySelector(".rerecord-mark")?.textContent ?? "").trim(),
    links: Array.from(tr.querySelectorAll("a")).map((a) => a.getAttribute("href") ?? ""),
  }));
  return { rows };
});

// Every badge says its state in words, from the known set; colour only repeats it.
export const clipBadgesCarryText = always(
  () =>
    clipPanel.current === null ||
    clipPanel.current.rows.every(
      (r) =>
        r.freshBadges.length === 1 &&
        r.freshBadges[0] === r.freshness &&
        FRESHNESS.includes(r.freshness) &&
        r.perfBadges.every((t) => RATINGS.includes(t)),
    ),
);

// redo first, then check, then unknown, then the rest; within one freshness, best rating first.
export const clipsSortedFreshnessFirst = always(() => {
  if (clipPanel.current === null) return true;
  const keys = clipPanel.current.rows.map((r) => [FRESHNESS.indexOf(r.freshness), RATINGS.indexOf(r.best)]);
  return keys.every((k, i) => i === 0 || keys[i - 1][0] < k[0] || (keys[i - 1][0] === k[0] && keys[i - 1][1] <= k[1]));
});

// A clip that is both redo and strong is marked in words, and only such a clip.
export const strongRedoIsMarked = always(
  () =>
    clipPanel.current === null ||
    clipPanel.current.rows.every(
      (r) => r.marked === (r.freshness === "redo" && r.best === "strong") && (!r.marked || r.markText === "re-record first"),
    ),
);

// Last-posted links go to https:// posts only.
export const clipLinksAreHttps = always(
  () => clipPanel.current === null || clipPanel.current.rows.every((r) => r.links.every((h) => h.startsWith("https://"))),
);

// Bombadil has no hover action: move the pointer over a chart the way a mouse would.
const hoverChart = registerCustomAction("hoverChart", async (document, window, index: number, frac: number) => {
  const charts = Array.from(document.querySelectorAll<SVGSVGElement>("svg.chart[data-values]"));
  const svg = charts[index % Math.max(charts.length, 1)];
  if (!svg) throw new Error("no chart");
  svg.scrollIntoView({ block: "center" });
  const box = svg.getBoundingClientRect();
  svg.dispatchEvent(new window.PointerEvent("pointermove", {
    bubbles: true, clientX: box.left + box.width * frac, clientY: box.top + box.height / 2,
  }));
});

// Opens a clip's "What changed" details, so the text inside is checked (contrast, raw markup) too.
const openWhy = registerCustomAction("openWhy", async (document, _window, index: number) => {
  const all = Array.from(document.querySelectorAll<HTMLDetailsElement>("table.clips details.why"));
  const details = all[index % Math.max(all.length, 1)];
  if (!details) throw new Error("no clip details");
  details.open = true;
  details.scrollIntoView({ block: "center" });
});

export const dashboardActions = actions(() =>
  dash.current === null
    ? []
    : [
        ...[0, 2, 5, 6].flatMap((i) => [0.02, 0.5, 0.98].map((x) => hoverChart(i, x))),
        ...(clipPanel.current && clipPanel.current.rows.length ? [0, 1, 2].map((i) => openWhy(i)) : []),
      ],
);

export const explore = weighted([
  [40, localClicks],
  [15, inputs],
  [5, scroll],
  [3, back],
  [2, reload],
  [25, reviewActions],
  [10, jump],
  [10, dashboardActions],
]);
