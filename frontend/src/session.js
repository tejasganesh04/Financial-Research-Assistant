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

// Tabs tell each other about signing out, so no tab keeps working on a session that has ended.
const channel =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel("financial-research-session")
    : null;

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

function remember(session) {
  accessToken = session.access_token;
  const lifetime = session.expires_in * 1000;
  expiresAt = Date.now() + lifetime;
  clearTimeout(renewTimer);
  // Renew a minute before expiry. Timers pause while a laptop sleeps; requests check expiry too.
  renewTimer = setTimeout(
    () => renewSession().catch(() => {}),
    Math.max(5000, lifetime - 60000),
  );
  return session.user;
}

export function forgetSession() {
  accessToken = null;
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
  return remember(response.data);
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
      } catch {
        forgetSession();
        window.dispatchEvent(new Event("session-expired"));
        return Promise.reject(error);
      }
      config.headers.Authorization = `Bearer ${accessToken}`;
      return client(config);
    }
    return Promise.reject(error);
  },
);
