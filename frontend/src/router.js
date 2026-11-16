import { useEffect, useState } from "react";

// A minimal path router: the server returns the same page for every app path (see src/api/main.py).

const listeners = new Set();

export function navigate(to, { replace = false } = {}) {
  if (to === window.location.pathname + window.location.search) return;
  window.history[replace ? "replaceState" : "pushState"]({}, "", to);
  listeners.forEach((listener) => listener());
}

export function useLocation() {
  const read = () => ({
    path: window.location.pathname.replace(/\/+$/, "") || "/",
    query: new URLSearchParams(window.location.search),
  });
  const [location, setLocation] = useState(read);
  useEffect(() => {
    const update = () => setLocation(read());
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return location;
}

// For <a href> links that should change the view without reloading the page.
export function linkHandler(to) {
  return (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    navigate(to);
  };
}
