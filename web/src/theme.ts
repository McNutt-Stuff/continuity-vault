// Light/dark theme control. Persisted per-browser in localStorage and applied to
// <html data-theme>; the CSS variables in theme.css do the rest.

export type Theme = "dark" | "light";

const KEY = "cv_theme";

export function getTheme(): Theme {
  return localStorage.getItem(KEY) === "light" ? "light" : "dark";
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(KEY, theme);
}

// Apply the saved theme before React renders to avoid a flash of the wrong theme.
export function initTheme(): void {
  applyTheme(getTheme());
}
