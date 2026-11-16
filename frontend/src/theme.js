// Light is the default whatever the device prefers; dark is a choice made in the account menu.
const KEY = "financial-research-theme";

export function storedTheme() {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "dark" ? "#1e1f20" : "#f8fafd");
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Private windows may refuse storage; the choice then lasts for this page only.
  }
}
