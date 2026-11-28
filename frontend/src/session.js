import axios from "axios";

// Sign-in state for this page.
//
// The access token (valid for an hour) lives only in this module's memory: never in localStorage or
// sessionStorage, so injected scripts or other tabs cannot read it from storage. The refresh token is
// an httpOnly cookie that JavaScript cannot read at all; the browser sends it only to /v1/auth.
// On load, and shortly before the access token expires, the page asks /v1/auth/refresh for a new one.

let accessToken = null;
let expiresAt = 0;
let renewTimer = null;
let inflight = null;
// The account this page is signed in as. The browser keeps one refresh cookie for every tab, so a
// sign-in to another account in any tab replaces this page's account too.
let account = null;
let replaced = false;

// Tabs tell each other about signing in and out, so no tab keeps working on a session that has
// ended or quietly carries on as a different person.
const channel =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel("financial-research-session")
    : null;

function replaceAccount(name) {
  forgetSession();
  replaced = true;
  window.dispatchEvent(
    new CustomEvent("account-replaced", { detail: { name } }),
  );
}

const replacedError = () =>
  Object.assign(
    new Error("This browser is now signed in to a different account."),
    { accountReplaced: true },
  );

channel?.addEventListener("message", (event) => {
  const data = event.data;
  if (data?.type === "signed-in" && account && data.user !== account)
    replaceAccount(data.name);
});

// Called with the other account's name when another tab signs this browser into it.
export function onAccountReplaced(handler) {
  const listener = (event) => handler(event.detail.name);
  window.addEventListener("account-replaced", listener);
  return () => window.removeEventListener("account-replaced", listener);
}

export function onSignedOutElsewhere(handler) {
  if (!channel) return () => {};
  const listener = (event) => {
    if (event.data === "signed-out") handler();
  };
  channel.addEventListener("message", listener);
  return () => channel.removeEventListener("message", listener);
}

// Requests to /v1 carry the access token; a 401 triggers one renewal and one retry.
export const client = axios.create({ baseURL: "/v1", timeout: 20000 });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function remember(session, { signedIn = false } = {}) {
  // A renewal that comes back as someone else means another tab signed in to a different account:
  // stop here rather than send this page's next request as that person.
  if (!signedIn && account && session.user.user_id !== account) {
    replaceAccount(session.user.name);
    throw replacedError();
  }
  account = session.user.user_id;
  accessToken = session.access_token;
  const lifetime = session.expires_in * 1000;
  expiresAt = Date.now() + lifetime;
  clearTimeout(renewTimer);
  // Renew a minute before expiry, or at three quarters of a short lifetime. Timers pause while a
  // laptop sleeps; requests check expiry too.
  renewTimer = setTimeout(
    () => renewSession().catch(() => {}),
    Math.max(5000, lifetime - 60000, lifetime * 0.75),
  );
  return session.user;
}

export function forgetSession() {
  accessToken = null;
  account = null;
  expiresAt = 0;
  clearTimeout(renewTimer);
}

export const hasSession = () => !!accessToken;

async function postRefresh() {
  const response = await axios.post("/v1/auth/refresh", null, {
    timeout: 15000,
  });
  return response.data;
}

// One renewal at a time, across tabs when the browser supports Web Locks: tabs share the refresh
// cookie, and a refresh token works only once.
export function renewSession() {
  // After another tab took over the browser, only a new sign-in (or a reload) continues here.
  if (replaced) return Promise.reject(replacedError());
  if (!inflight) {
    const run = async () => {
      try {
        return remember(await postRefresh());
      } catch (error) {
        // Another tab may have renewed a moment ago; the browser now holds the newer cookie.
        if (error.response?.status === 401 && !navigator.locks) {
          await sleep(500);
          return remember(await postRefresh());
        }
        throw error;
      }
    };
    const locked = navigator.locks?.request
      ? () => navigator.locks.request("financial-research-session", run)
      : run;
    inflight = locked().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

export async function signIn(username, password) {
  const response = await axios.post(
    "/v1/auth/login",
    { username, password },
    { timeout: 20000 },
  );
  replaced = false;
  const user = remember(response.data, { signedIn: true });
  channel?.postMessage({
    type: "signed-in",
    user: user.user_id,
    name: user.name,
  });
  return user;
}

export async function signOut() {
  forgetSession();
  channel?.postMessage("signed-out");
  await axios.post("/v1/auth/logout", null, { timeout: 10000 }).catch(() => {});
}

// Used after a password change, which ends every older session and starts a new one here.
export const adoptSession = (session) => remember(session);

const AUTH_ENDPOINTS = ["/auth/login", "/auth/refresh", "/auth/logout"];

client.interceptors.request.use(async (config) => {
  if (accessToken && Date.now() > expiresAt - 15000) {
    await renewSession().catch(() => {});
  }
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = error.config;
    if (
      error.response?.status === 401 &&
      config &&
      !config._renewed &&
      !AUTH_ENDPOINTS.some((path) => config.url?.startsWith(path))
    ) {
      config._renewed = true;
      try {
        await renewSession();
      } catch (renewal) {
        forgetSession();
        // A replaced account already has its own message; this is not an expiry.
        if (!renewal?.accountReplaced)
          window.dispatchEvent(new Event("session-expired"));
        return Promise.reject(error);
      }
      config.headers.Authorization = `Bearer ${accessToken}`;
      return client(config);
    }
    return Promise.reject(error);
  },
);
