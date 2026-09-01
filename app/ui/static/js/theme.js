/**
 * VEditor Theme & Customization Configuration
 * Supports Light Mode (Default) and Dark Mode with persistence.
 */

window.VEditorConfig = window.VEditorConfig || {
  appName: "VEditor",
  logoText: "VEditor",
  seekSmall: 5,      // 5 seconds
  seekMedium: 30,    // 30 seconds
  seekBig: 60,       // 1 minute (for long talks)
  seekMega: 300,     // 5 minutes (for multi-hour recordings)
  frameRate: 25,
  pollingIntervalMs: 5000,
};

// ── Theme Manager ───────────────────────────────────────────────
(function () {
  function getPreferredTheme() {
    const saved = localStorage.getItem('veditor_theme');
    if (saved === 'dark' || saved === 'light') return saved;
    // Default is light mode
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('veditor_theme', theme);
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) {
      btn.title = theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';
      const sunIcon = document.getElementById('theme-icon-sun');
      const moonIcon = document.getElementById('theme-icon-moon');
      if (sunIcon && moonIcon) {
        sunIcon.style.display = theme === 'dark' ? 'block' : 'none';
        moonIcon.style.display = theme === 'dark' ? 'none' : 'block';
      }
    }
  }

  window.toggleTheme = function () {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
  };

  // Immediate init before DOM paints to prevent flash
  const initial = getPreferredTheme();
  document.documentElement.setAttribute('data-theme', initial);

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(initial);
  });
})();
