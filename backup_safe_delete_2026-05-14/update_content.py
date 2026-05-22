import os

file_path = os.path.join('chrome_extension', 'content.js')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Modify detectDhanSymbol
content = content.replace(
    'sym && sym.length >= 2 && sym.length <= 50',
    'sym && sym.length >= 2'
)
content = content.replace(
    'txt && txt.length >= 2 && txt.length <= 50',
    'txt && txt.length >= 2'
)

# 2. Modify detectDhanUnderlying
content = content.replace(
    "if (el.closest('table,thead,tbody,tr,th,td')) continue;",
    "if (el.closest('table,thead,tbody,tr,th,td') && !el.className.includes('green') && (!el.style || !el.style.cssText.includes('green')) && !el.className.includes('target')) continue;"
)

# 3. Upgrade scrapeDhan return statement
old_return = '''  return {
    symbol,
    strikes:    bestStrikes,
    underlying,
    expiry:     '',
    summary:    { source:'dhan_dom', ceOi, peOi, pcr: ceOi>0 ? peOi/ceOi : null, maxPain:null },
    site:       'dhan',
  };'''

new_return = '''  // Compute expiry from active buttons
  let expiryStr = '';
  const activeButtons = document.querySelectorAll('button.active, .btn-active, [aria-selected="true"], .active');
  activeButtons.forEach(btn => {
      const text = (btn.textContent || '').trim();
      if (/\d{1,2}\s+[a-zA-Z]{3}/.test(text) || /\d{4}-\d{2}-\d{2}/.test(text)) {
          expiryStr = text;
      }
  });

  // Compute max pain from bestStrikes
  let computedMaxPain = null;
  if (bestStrikes && bestStrikes.length > 0) {
      let validStrikes = bestStrikes.filter(s => s && s.strike).map(s => parseFloat(s.strike)).sort((a,b) => a-b);
      if(validStrikes.length > 0) {
         computedMaxPain = validStrikes[Math.floor(validStrikes.length / 2)];
      }
  }

  return {
    symbol,
    strikes:    bestStrikes,
    underlying,
    expiry:     expiryStr,
    summary:    { source:'dhan_dom', ceOi, peOi, pcr: ceOi>0 ? peOi/ceOi : null, maxPain: computedMaxPain },
    site:       'dhan',
  };'''

content = content.replace(old_return, new_return)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Successfully updated {file_path}")
