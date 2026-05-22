import requests, re

with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    html = f.read()

print('=== Select / Dropdown elements ===')
for m in re.finditer(r'<select[^>]*>(.*?)</select>', html, re.DOTALL | re.IGNORECASE):
    tag = m.group(0).replace('\n',' ')[:600]
    print(tag)
    print('---')

print('\n=== Symbol-related classes/ids ===')
for pat in [r'class="[^"]*symbol[^"]*"', r'id="[^"]*symbol[^"]*"', r'class="[^"]*dropdown[^"]*"', r'class="[^"]*select[^"]*"']:
    matches = re.findall(pat, html, re.IGNORECASE)
    if matches:
        print('Found:', set(matches[:15]))
        break

url = 'https://www.research360.in/fno/option/ajax/optionChainApi.php'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.research360.in/future-and-options/option-chain',
    'X-Requested-With': 'XMLHttpRequest',
}

print('\n=== API GET ===')
try:
    r = requests.get(url, headers=headers, timeout=20)
    print('GET status:', r.status_code)
    print('GET content-type:', r.headers.get('Content-Type'))
    print('GET first 500 chars:', r.text[:500])
except Exception as e:
    print('GET error:', e)

print('\n=== API POST symbol=NIFTY ===')
try:
    r = requests.post(url, headers=headers, data={'symbol':'NIFTY'}, timeout=20)
    print('POST status:', r.status_code)
    print('POST content-type:', r.headers.get('Content-Type'))
    print('POST first 1200 chars:', r.text[:1200])
except Exception as e:
    print('POST error:', e)
