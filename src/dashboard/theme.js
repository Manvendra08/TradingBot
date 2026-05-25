/**
 * Global Theme Manager — single, simple, reliable
 * Loaded in <head>: applies theme immediately to avoid flash.
 * Button wiring happens after DOMContentLoaded.
 */
(function () {
  const KEY   = 'nsebot-theme';
  const LIGHT = 'light';
  const DARK  = 'dark';

  /* ── Apply saved theme immediately (before paint) ── */
  const saved = localStorage.getItem(KEY) || DARK;
  if (saved === LIGHT) {
    document.documentElement.setAttribute('data-theme', LIGHT);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }

  /* ── Toggle function exposed globally ── */
  window.toggleTheme = function () {
    const current = document.documentElement.getAttribute('data-theme') === LIGHT ? LIGHT : DARK;
    const next    = current === LIGHT ? DARK : LIGHT;

    if (next === LIGHT) {
      document.documentElement.setAttribute('data-theme', LIGHT);
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem(KEY, next);

    /* Update button icon */
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) btn.textContent = next === LIGHT ? '🌙' : '☀️';

    /* Notify any chart redraw listeners */
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
  };

  /* ── Wire button after DOM is ready ── */
  function wireButton() {
    const btn = document.getElementById('theme-toggle-btn');
    if (!btn) return;
    const current = document.documentElement.getAttribute('data-theme') === LIGHT ? LIGHT : DARK;
    btn.textContent = current === LIGHT ? '🌙' : '☀️';
    btn.onclick = window.toggleTheme;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireButton);
  } else {
    wireButton();
  }

  /* Keep legacy window.themeManager API working */
  window.themeManager = { toggleTheme: window.toggleTheme };
})();
