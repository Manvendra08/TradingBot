import requests, json, re

# 1. Probe API with correct params
url = 'https://www.research360.in/fno/option/ajax/optionChainApi.php'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.research360.in/future-and-options/option-chain',
    'X-Requested-With': 'XMLHttpRequest',
}
payload = {
    'stock': 'NIFTY50',
    'expiry': '2026-05-05',
    'showall': '',
    'showallnew': '',
}
print('=== API Probe ===')
try:
    r = requests.post(url, headers=headers, data=payload, timeout=20)
    print('Status:', r.status_code)
    ct = r.headers.get('Content-Type','')
    print('Content-Type:', ct)
    if 'json' in ct.lower():
        data = r.json()
        print('JSON keys:', list(data.keys())[:20])
        for k in data:
            v = data[k]
            preview = str(v)[:300].replace('\n',' ')
            print(f'{k}: {preview}')
    else:
        print('Non-JSON response first 800 chars:')
        print(r.text[:800])
except Exception as e:
    print('Error:', e)

# 2. Search raw HTML for f_ classes and OI containers
print('\n=== f_* classes in raw HTML ===')
with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    html = f.read()
classes = set(re.findall(r'class="([^"]*\bf_[^"]*)"', html, re.IGNORECASE))
for c in sorted(classes):
    print(c)

print('\n=== call_oi / put_oi / total_oi containers ===')
for kw in ['call_oi','put_oi','total_oi','callOi','putOi','totalOi']:
    idx = html.lower().find(kw)
    if idx != -1:
        start = max(idx-200, 0)
        end = min(idx+200, len(html))
        print(f'--- {kw} ---')
        print(html[start:end].replace('\n',' '))
        print()
