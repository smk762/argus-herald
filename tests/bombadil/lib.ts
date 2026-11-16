// Shared properties and actions for the review UI specs.
import { always } from "@antithesishq/bombadil";
import { extract, registerCustomAction } from "@antithesishq/bombadil/browser";
import { isAction } from "@antithesishq/bombadil/actions";
import { clicks as defaultClicks } from "@antithesishq/bombadil/browser/defaults/actions";

export {
  noUncaughtExceptions,
  noUnhandledPromiseRejections,
} from "@antithesishq/bombadil/browser/defaults/properties";

const navigationStatus = extract((state) => {
  const entry = state.window.performance.getEntriesByType("navigation")[0];
  return entry && entry instanceof PerformanceNavigationTiming ? entry.responseStatus : null;
});

// 4xx answers are part of the design (a wrong token is 401, a missing draft 404, an expired form
// 400). A 5xx never is.
export const noServerErrors = always(() => (navigationStatus.current ?? 0) < 500);

const consoleErrors = extract((state) =>
  state.console
    .filter((entry) => entry.level === "error")
    .map((entry) => entry.args.map((arg) => (typeof arg === "string" ? arg : JSON.stringify(arg))).join(" "))
    // Chrome logs every 4xx document as "Failed to load resource"; those are covered above.
    .filter((text) => !/Failed to load resource: the server responded with a status of 4\d\d/.test(text)),
);

export const noConsoleErrors = always(() => consoleErrors.current.length === 0);

// Only same-origin links: the tests never reach X, PyPI or Patreon.
export const localClicks = defaultClicks.filter((action) => {
  if (!isAction(action, "Click")) return true;
  const href = action.Click.fingerprint.href;
  return !href || href.startsWith("/") || href.startsWith("#") || href.startsWith("?");
});

export const goto = registerCustomAction("goto", async (_document, window, path: string) => {
  window.location.assign(path);
});

// A form the page never offered: posts straight to an endpoint, the way a script would.
export const forgePost = registerCustomAction(
  "forgePost",
  async (document, _window, path: string, fields: { [name: string]: string }) => {
    const form = document.createElement("form");
    form.method = "post";
    form.action = path;
    for (const [name, value] of Object.entries(fields)) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
  },
);

export const PATHS = [
  "/",
  "/?status=open",
  "/?status=approved",
  "/?status=all",
  "/compose",
  "/digests",
  "/broadcasts",
  "/drafts/999",
  ...Array.from({ length: 12 }, (_, i) => `/drafts/${i + 1}`),
];
