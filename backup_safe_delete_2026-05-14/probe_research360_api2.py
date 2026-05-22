import requests, json, re, traceback

out_path = 'probe_research360_api_out2.txt'
out = open(out_path, 'w', encoding='utf-8')

def log(msg):
    out.write(str(msg) + '\n')
    out.flush()

url = 'https://www.research360.in/fno/option/ajax/optionChainApi.php'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.research360.in/future-and-options/option-chain',
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Origin': 'https://www.research360.in',
}

# Use session to carry cookies from landing page
session = requests.Session()
log('=== Fetching landing page for cookies ===')
try:
    r0 = session.get('https://www.research360.in/future-and-options/option-chain', headers=headers, timeout=20)
    log(f'Landing status: {r0.status_code}')
    log(f'Landing cookies: {dict(session.cookies)}')
except Exception as e:
    log(f'Landing error: {e}')
    log(traceback.format_exc())

payload = {
    'stock': 'NIFTY50',
    'expiry': '2026-05-05',
    'showall': '',
    'showallnew': '',
}

log('\n=== API POST ===')
try:
    r = session.post(url, headers=headers, data=payload, timeout=20)
    log(f'API status: {r.status_code}')
    log(f'API content-type: {r.headers.get("Content-Type")}')
    log(f'API first 1000 chars: {r.text[:1000]}')
    if r.headers.get('Content-Type','').startswith('application/json'):
        data = r.json()
        log(f'JSON keys: {list(data.keys())}')
        for k in data:
            v = data[k]
            preview = str(v)[:400].replace('\n',' ')
            log(f'{k}: {preview}')
    else:
        log('Response is not JSON')
except Exception as e:
    log(f'API error: {e}')
    log(traceback.format_exc())

# Also try with today's date as expiry? The HTML had 2026-05-05 selected, but maybe dynamic.
# Search raw HTML for all expiry values to use correct one.
with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    html = f.read()
expiry_values = re.findall(r'<option[^>]*value="(\d{4}-\d{2}-\d{2})"[^>]*>', html)
log(f'\nExpiry values found in HTML: {list(set(expiry_values))}')

# Search for f_ classes again
classes = set(re.findall(r'class="([^"]*\bf_[^"]*)"', html, re.IGNORECASE))
log(f'\nf_* classes: {sorted(classes)}')

# Search for OI total elements (maybe outside f_)
for kw in ['call_oi','put_oi','total_oi','callOi','putOi','totalOi','callOpenInterest','putOpenInterest']:
    idx = html.lower().find(kw)
    if idx != -1:
        start = max(idx-200, 0)
        end = min(idx+200, len(html))
        log(f'--- {kw} context ---')
        log(html[start:end].replace('\n',' '))
        log('')

log('\n=== Done ===')
out.close()
