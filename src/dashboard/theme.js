/**
 * Global Theme Manager
 * Handles day/night theme switching across all dashboard pages
 * Usage: Include in <head> before other scripts
 */

class ThemeManager {
  constructor() {
    this.STORAGE_KEY = 'nsebot-theme';
    this.DARK_THEME = 'dark';
    this.LIGHT_THEME = 'light';
    this.init();
  }

  init() {
    // Load saved theme or detect system preference
    const saved = localStorage.getItem(this.STORAGE_KEY);
    const theme = saved || this.getSystemTheme();
    this.setTheme(theme);
    this.createToggleButton();
  }

  getSystemTheme() {
    // Check if system prefers dark mode
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return this.DARK_THEME;
    }
    return this.DARK_THEME; // Default to dark
  }

  setTheme(theme) {
    const html = document.documentElement;
    if (theme === this.LIGHT_THEME) {
      html.setAttribute('data-theme', this.LIGHT_THEME);
    } else {
      html.removeAttribute('data-theme');
    }
    localStorage.setItem(this.STORAGE_KEY, theme);
    this.updateToggleButton(theme);
  }

  toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || this.DARK_THEME;
    const next = current === this.DARK_THEME ? this.LIGHT_THEME : this.DARK_THEME;
    this.setTheme(next);
  }

  createToggleButton() {
    // Check if button already exists
    if (document.getElementById('theme-toggle-btn')) {
      this.updateToggleButton(document.documentElement.getAttribute('data-theme') || this.DARK_THEME);
      return;
    }

    // Find header or create one
    let header = document.querySelector('header');
    if (!header) {
      header = document.createElement('header');
      document.body.insertBefore(header, document.body.firstChild);
    }

    // Create toggle button
    const btn = document.createElement('button');
    btn.id = 'theme-toggle-btn';
    btn.className = 'theme-toggle';
    btn.setAttribute('title', 'Toggle dark/light theme');
    btn.style.marginLeft = 'auto';
    btn.style.marginRight = '12px';
    btn.onclick = () => this.toggleTheme();

    // Add to header (right side)
    header.appendChild(btn);
    this.updateToggleButton(document.documentElement.getAttribute('data-theme') || this.DARK_THEME);
  }

  updateToggleButton(theme) {
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) {
      btn.textContent = theme === this.LIGHT_THEME ? '🌙' : '☀️';
      btn.onclick = () => this.toggleTheme();
    }
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    window.themeManager = new ThemeManager();
  });
} else {
  window.themeManager = new ThemeManager();
}
