import os

file_path = os.path.join('chrome_extension', 'popup.js')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

old_func = '''function toFinite(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }'''

new_func = '''function toFinite(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }'''

content = content.replace(old_func, new_func)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
