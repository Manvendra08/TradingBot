# Theme Switching Fix - Paper Trading Page

## Problem
Theme toggle was not working on the paper trading page (`/paper`). The theme button was missing and Chart.js colors were hardcoded to dark theme.

## Root Causes
1. **Missing Theme Toggle Button**: paper.html didn't have the theme toggle button in the header
2. **Hardcoded Chart Colors**: Chart.js defaults were set once at page load and never updated when theme changed
3. **No Theme Change Observer**: No mechanism to redraw charts when theme changed

## Solution

### 1. Added Theme Toggle Button to Header
**File**: `src/dashboard/paper.html`
- Added `<button id="theme-toggle-btn" class="theme-toggle" title="Toggle dark/light theme">☀️</button>` to the header
- Button is positioned at the right end of the header using flexbox

### 2. Made Chart.js Theme-Aware
**File**: `src/dashboard/paper.html`
- Created `updateChartDefaults()` function that detects current theme and sets Chart.js colors accordingly
- Dark theme: `#7a8796` text, `#1e2530` borders
- Light theme: `#334155` text, `#cbd5e1` borders

### 3. Added Theme Change Observer
**File**: `src/dashboard/paper.html`
- Created MutationObserver to watch for `data-theme` attribute changes on `<html>`
- When theme changes:
  - Updates Chart.js defaults
  - Destroys and redraws equity chart with new colors
  - Stores last equity data for redraw

### 4. Enhanced Chart Colors
**File**: `src/dashboard/paper.html`
- Equity chart line color changes based on theme:
  - Dark: `#00e5a0` (bright green)
  - Light: `#0891b2` (cyan-blue)
- Tooltip colors adapt to theme
- Grid colors adapt to theme
- Point colors adapt to theme

### 5. Improved Theme Manager
**File**: `src/dashboard/theme.js`
- Enhanced `createToggleButton()` to handle pre-existing buttons in HTML
- Updated `updateToggleButton()` to attach click handler to existing button
- Prevents duplicate button creation

## Changes Made

### paper.html
```html
<!-- Added to header -->
<button id="theme-toggle-btn" class="theme-toggle" title="Toggle dark/light theme">☀️</button>

<!-- Added to script -->
function updateChartDefaults() {
  const isDark = !document.documentElement.getAttribute('data-theme') || document.documentElement.getAttribute('data-theme') === 'dark';
  Chart.defaults.color = isDark ? '#7a8796' : '#334155';
  Chart.defaults.borderColor = isDark ? '#1e2530' : '#cbd5e1';
}

// MutationObserver to watch theme changes
const observer = new MutationObserver(() => {
  updateChartDefaults();
  if (eqChart) {
    eqChart.destroy();
    eqChart = null;
    const rows = window.lastEquityData || [];
    if (rows.length) renderEquity(rows);
  }
});
observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
```

### theme.js
```javascript
// Enhanced to handle pre-existing buttons
createToggleButton() {
  if (document.getElementById('theme-toggle-btn')) {
    this.updateToggleButton(document.documentElement.getAttribute('data-theme') || this.DARK_THEME);
    return;
  }
  // ... create button if not found
}

updateToggleButton(theme) {
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) {
    btn.textContent = theme === this.LIGHT_THEME ? '🌙' : '☀️';
    btn.onclick = () => this.toggleTheme();
  }
}
```

## Testing

### Dark Theme
- ✅ Theme toggle button visible in header
- ✅ Clicking button switches to light theme
- ✅ Chart colors: green line, dark grid
- ✅ Text colors: light gray
- ✅ Theme persists on page reload

### Light Theme
- ✅ Theme toggle button visible in header
- ✅ Clicking button switches to dark theme
- ✅ Chart colors: cyan line, light grid
- ✅ Text colors: dark blue-gray
- ✅ Theme persists on page reload

### Chart Redraw
- ✅ Equity chart redraws when theme changes
- ✅ Chart colors update correctly
- ✅ No data loss during redraw
- ✅ Smooth transition

## Files Modified
- `src/dashboard/paper.html` — Added theme toggle button and theme-aware chart logic
- `src/dashboard/theme.js` — Enhanced button creation and update logic

## Commit
- **Hash**: `2b66b2c5`
- **Message**: "Fix theme switching on paper trading page - add theme toggle button and make chart colors theme-aware"
- **Status**: ✅ Pushed to GitHub

## Verification
Theme switching now works on both:
- ✅ Dashboard page (`/`)
- ✅ Paper trading page (`/paper`)

Both pages now have:
- Theme toggle button in header
- Persistent theme preference
- All UI elements adapt to theme
- Charts update colors on theme change
