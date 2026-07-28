with open(r'C:\Users\manve\Downloads\NSEBOT\src\engine\llm_enrichment.py', 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
idx = content.find('if _is_mcx:')
with open(r'C:\Users\manve\Downloads\NSEBOT\scratch\mcx_nse_lines.txt', 'w', encoding='utf-8') as out:
    out.write(content[idx:idx+900])
print('written', len(content[idx:idx+900]))