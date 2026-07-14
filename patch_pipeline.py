content = open('src/engine/pipeline.py', 'r', encoding='utf-8').read()
content = content.replace('            market_state=None,\n', '')
open('src/engine/pipeline.py', 'w', encoding='utf-8').write(content)
