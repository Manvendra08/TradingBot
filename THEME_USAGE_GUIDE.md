# Theme Usage Guide

## Quick Start

### Switching Themes

1. **Look for the theme toggle button** in the top-right corner of any page
   - 🌙 **Moon icon** = Currently in Dark theme (click to switch to Light)
   - ☀️ **Sun icon** = Currently in Light theme (click to switch to Dark)

2. **Click the button** to instantly switch themes
3. **Your choice is saved** — theme persists across page reloads

---

## Dark Theme (Default)

### When to Use:
- Night trading sessions
- Reduced eye strain in low-light environments
- Professional trading appearance

### Colors:
```
Background:  Deep dark (#0a0c0f)
Cards:       Dark gray (#111418)
Text:        Light gray (#e2e8f0)
Accent:      Bright green (#00e5a0)
```

### Visual Example:
```
┌─────────────────────────────────────────┐
│ 📊 PAPER TRADING              🌙        │  ← Click moon to switch
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────────┐  ┌──────────────┐   │
│  │ TOTAL TRADES │  │  WIN RATE    │   │
│  │      5       │  │   100.0%     │   │
│  └──────────────┘  └──────────────┘   │
│                                         │
│  [Dark background with light text]     │
│                                         │
└─────────────────────────────────────────┘
```

---

## Light Theme

### When to Use:
- Daytime trading sessions
- Bright office environments
- Presentations or sharing screens

### Colors:
```
Background:  Off-white (#f8fafc)
Cards:       White (#ffffff)
Text:        Dark blue (#0f172a)
Accent:      Dark green (#059669)
```

### Visual Example:
```
┌─────────────────────────────────────────┐
│ 📊 PAPER TRADING              ☀️        │  ← Click sun to switch
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────────┐  ┌──────────────┐   │
│  │ TOTAL TRADES │  │  WIN RATE    │   │
│  │      5       │  │   100.0%     │   │
│  └──────────────┘  └──────────────┘   │
│                                         │
│  [Light background with dark text]     │
│                                         │
└─────────────────────────────────────────┘
```

---

## Theme Persistence

### How It Works:
1. **First Visit**: System detects your OS preference (dark/light mode)
2. **Your Choice**: When you click the toggle, your preference is saved
3. **Next Visit**: Your chosen theme loads automatically
4. **Storage**: Saved in browser's localStorage (no server needed)

### Clear Theme Preference:
If you want to reset to system default:
```javascript
// Open browser console (F12) and run:
localStorage.removeItem('nsebot-theme');
location.reload();
```

---

## Color Comparison

### Dark Theme vs Light Theme

| Element | Dark | Light |
|---------|------|-------|
| Background | #0a0c0f | #f8fafc |
| Cards | #111418 | #ffffff |
| Borders | #1e2530 | #e2e8f0 |
| Text | #e2e8f0 | #0f172a |
| Accent | #00e5a0 | #059669 |
| Profit (Green) | #00e5a0 | #059669 |
| Loss (Red) | #ff4d6d | #e11d48 |
| Warning (Yellow) | #ffd166 | #d97706 |

---

## Accessibility

### Dark Theme:
- ✅ Reduces eye strain in low-light
- ✅ Better for OLED screens
- ✅ Professional appearance
- ⚠️ May be harder to read in bright sunlight

### Light Theme:
- ✅ Better in bright environments
- ✅ Easier to read in sunlight
- ✅ Professional appearance
- ⚠️ May cause eye strain in dark rooms

### Recommendation:
- **Use Dark theme** for evening/night trading
- **Use Light theme** for daytime trading
- **Switch as needed** — it's instant!

---

## Pages with Theme Support

### Currently Supported:
- ✅ Paper Trading Dashboard (`/paper`)
- ✅ Main Dashboard (`/`)

### How to Add Theme to Other Pages:

Add these two lines to the `<head>` section:

```html
<link rel="stylesheet" href="theme.css">
<script src="theme.js"></script>
```

Example:
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>My Page</title>
  
  <!-- Add these two lines -->
  <link rel="stylesheet" href="theme.css">
  <script src="theme.js"></script>
  
  <!-- Your other styles -->
  <style>
    /* Your custom styles here */
  </style>
</head>
<body>
  <!-- Your content -->
</body>
</html>
```

---

## Customizing Colors

### Edit Theme Colors:

1. Open `src/dashboard/theme.css`
2. Find the `:root` section (dark theme):
   ```css
   :root {
     --bg: #0a0c0f;           /* Change background color */
     --surface: #111418;      /* Change card color */
     --accent: #00e5a0;       /* Change accent color */
     /* ... etc ... */
   }
   ```

3. Or find `[data-theme="light"]` section (light theme):
   ```css
   [data-theme="light"] {
     --bg: #f8fafc;           /* Light background */
     --accent: #059669;       /* Light accent */
     /* ... etc ... */
   }
   ```

4. Change the hex color codes to your preference
5. Save and refresh the page

---

## Troubleshooting

### Theme Toggle Not Appearing?
- Check browser console (F12) for errors
- Ensure `theme.js` is loaded
- Try hard refresh (Ctrl+Shift+R)

### Theme Not Persisting?
- Check if localStorage is enabled
- Try clearing browser cache
- Check if cookies/storage are blocked

### Colors Look Wrong?
- Ensure `theme.css` is loaded
- Check for CSS conflicts with other stylesheets
- Try hard refresh (Ctrl+Shift+R)

### Reset to Default:
```javascript
// Open console (F12) and run:
localStorage.clear();
location.reload();
```

---

## Browser Support

### Supported Browsers:
- ✅ Chrome/Chromium (v90+)
- ✅ Firefox (v88+)
- ✅ Safari (v14+)
- ✅ Edge (v90+)
- ✅ Opera (v76+)

### Mobile Support:
- ✅ iOS Safari
- ✅ Android Chrome
- ✅ Android Firefox

---

## Tips & Tricks

### 1. **Keyboard Shortcut** (Future Enhancement)
Currently: Click the button
Future: Could add keyboard shortcut (e.g., Ctrl+Shift+T)

### 2. **Schedule Theme Changes** (Future Enhancement)
Could automatically switch themes based on time:
- 6 AM - 6 PM: Light theme
- 6 PM - 6 AM: Dark theme

### 3. **Custom Themes** (Future Enhancement)
Could add more themes:
- High Contrast (for accessibility)
- Colorblind-friendly
- Custom user themes

### 4. **Per-Page Theme Override** (Future Enhancement)
Could allow different themes for different pages

---

## FAQ

**Q: Will my theme choice be saved if I clear cookies?**
A: No, localStorage is separate from cookies. Your theme will be saved unless you specifically clear localStorage.

**Q: Can I use different themes on different devices?**
A: Yes! Each device/browser has its own localStorage, so you can have different themes on different devices.

**Q: Does theme switching affect my data?**
A: No, theme is purely visual. All your trading data remains unchanged.

**Q: Can I set a default theme for all users?**
A: Yes, modify the `getSystemTheme()` function in `theme.js` to return your preferred default.

**Q: Is there a way to force a specific theme?**
A: Yes, add `data-theme="light"` or `data-theme="dark"` to the `<html>` tag.

---

## Summary

- 🌙 **Click the moon/sun button** to toggle themes
- 💾 **Your choice is saved** automatically
- 🎨 **Two professional themes** included
- 📱 **Works on all devices** and browsers
- ⚡ **Instant switching** with smooth transitions
- 🔧 **Easy to customize** colors in CSS

Enjoy your trading with the perfect theme! 🚀
