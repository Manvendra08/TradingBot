import re
with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    html = f.read()

# 1. Find how optionChainApi.php is called (look for the URL in script tags)
print('=== optionChainApi.php call context ===')
idx = html.find('optionChainApi.php')
if idx != -1:
    start = max(idx-400, 0)
    end = min(idx+400, len(html))
    print(html[start:end].replace('\n',' '))
else:
    print('URL not found in raw HTML (maybe in external JS)')

# 2. Find all fno_* classes
print('\n=== fno_* classes ===')
classes = set(re.findall(r'class="([^"]*fno_[^"]*)"', html, re.IGNORECASE))
for c in sorted(classes):
    print(c)

# 3. Find text near Call OI / Put OI totals
print('\n=== Call OI / Put OI total containers ===')
for kw in ['Call OI','Put OI','Total Call','Total Put','Call Open Interest','Put Open Interest']:
    idx = html.find(kw)
    if idx != -1:
        start = max(idx-200, 0)
        end = min(idx+200, len(html))
        print(f'--- {kw} ---')
        print(html[start:end].replace('\n',' '))
        print()

# 4. Find any data-symbol or data-value attributes
print('=== data-* attributes near metrics ===')
for m in re.finditer(r'<[^>]*data-(symbol|value|oi|pcr)=[^>]*>', html, re.IGNORECASE):
    tag = m.group(0).replace('\n',' ')[:300]
    if 'fno_' in tag or 'oi' in tag or 'pcr' in tag:
        print(tag)
