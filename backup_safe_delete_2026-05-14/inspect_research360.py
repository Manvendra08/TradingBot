import re
with open('research360_raw.html','r',encoding='utf-8',errors='ignore') as f:
    text = f.read()
print('Length:', len(text))
keywords = ['PCR','Put Call Ratio','Total Call OI','Total Put OI','Call OI','Put OI','NIFTY','option-chain','tbody','thead','table']
found_any = False
for kw in keywords:
    for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
        start = max(m.start()-200, 0)
        end = min(m.end()+200, len(text))
        snippet = text[start:end].replace('\n',' ')
        print(f'--- {kw} at {m.start()} ---')
        print(snippet)
        print()
        found_any = True
        break
if not found_any:
    print('No keywords found.')
