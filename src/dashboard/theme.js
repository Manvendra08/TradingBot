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
    this.setupToggleButton();
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

  setupToggleButton() {
    // Wait for DOM to be ready
    const setupButton = () => {
      const btn = document.getElementById('theme-toggle-btn');
      if (btn) {
        const currentTheme = document.documentElement.getAttribute('data-theme') || this.DARK_THEME;
        this.updateToggleButton(currentTheme);
        // Bind the toggleTheme method to this instance
        btn.onclick = () => this.toggleTheme();
      }
    };

    // If DOM is already loaded, setup immediately
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', setupButton);
    } else {
      setupButton();
    }
  }

  updateToggleButton(theme) {
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) {
      btn.textContent = theme === this.LIGHT_THEME ? '🌙' : '☀️';
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

