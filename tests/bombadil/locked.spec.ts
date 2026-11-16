// With ARGUS_HERALD_UI_TOKEN set, nothing but the sign-in page is reachable without the token.
// Bombadil is never given the token: it types guesses, follows links, jumps to every route and
// posts forged forms to the action endpoints.
import { always } from "@antithesishq/bombadil";
import { actions, extract, weighted } from "@antithesishq/bombadil/browser";
import { back, inputs, reload } from "@antithesishq/bombadil/browser/defaults/actions";
import { PATHS, forgePost, goto, localClicks } from "./lib.ts";

export { noConsoleErrors, noServerErrors, noUncaughtExceptions, noUnhandledPromiseRejections } from "./lib.ts";

const view = extract((state) => {
  const doc = state.document;
  const text = doc.body ? doc.body.innerText : "";
  return {
    path: state.window.location.pathname,
    login: doc.querySelector("input[name=token]") !== null,
    expired: text.trim() === "This form expired. Reload the page and try again.",
    // Anything from the seeded store: package names, post text, platform pages.
    leaks: /argus-(atlas|lens)|zebra-canyon|Drafted by|Publish|Approve/.test(text),
  };
});

export const onlySignInWithoutToken = always(
  () => view.current.login || view.current.expired || view.current.path.startsWith("/static/"),
);

export const noDraftContentWithoutToken = always(() => !view.current.leaks);

const ENDPOINTS = [
  "/drafts/1/approve",
  "/drafts/2/publish",
  "/drafts/3/accept",
  "/drafts/5/schedule",
  "/drafts/1/save",
  "/media/1/alt",
  "/releases/approve",
  "/compose",
  "/broadcasts",
];

export const jump = actions(() => [...PATHS, "/media/1", "/static/review.css"].map((path) => goto(path)));
export const forge = actions(() => ENDPOINTS.map((path) => forgePost(path, { csrf: "guess", body: "forged" })));

export const explore = weighted([
  [30, localClicks],
  [30, inputs],
  [3, back],
  [2, reload],
  [20, jump],
  [15, forge],
]);
