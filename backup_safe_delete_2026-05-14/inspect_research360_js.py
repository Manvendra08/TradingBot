import re, json
with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    text = f.read()

# 1. Find large window.* assignments
print('=== Window assignments ===')
for m in re.finditer(r'window\.([A-Za-z0-9_$]+)\s*=\s*(\{.*?\});', text, re.DOTALL):
    name = m.group(1)
    val = m.group(2)
    if len(val) > 500:
        print(f'window.{name} = ... ({len(val)} chars)')
        # Try to pretty print a snippet
        try:
            data = json.loads(val)
            print(json.dumps(data, indent=2)[:2000])
        except Exception as e:
            print(val[:500])
        print('---')
        break  # just show first big one

# 2. Find script tags with optionChain or PCR keywords
print('\n=== Script tags with keywords ===')
for m in re.finditer(r'<script[^>]*>(.*?)</script>', text, re.DOTALL | re.IGNORECASE):
    script = m.group(1)
    if any(k in script for k in ['PCR','optionChain','option_chain','oiData','callOi','putOi']):
        snippet = script.strip().replace('\n',' ')[:800]
        print('SCRIPT SNIPPET:', snippet)
        print('---')

# 3. Find fetch/XMLHttpRequest URLs
print('\n=== API endpoints ===')
urls = re.findall(r'["\'](https?://[^"\']+?/[^"\']*option[^"\']*?)["\']', text, re.IGNORECASE)
urls += re.findall(r'["\'](/[^"\']*option[^"\']*?)["\']', text, re.IGNORECASE)
urls += re.findall(r'["\'](https?://[^"\']+?/[^"\']*api[^"\']*?)["\']', text, re.IGNORECASE)
for u in set(urls):
    print(u)
