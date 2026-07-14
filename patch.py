content = open('tests/test_timeframe_strategy.py', 'r', encoding='utf-8').read()
old = '    with (\n        patch(\"src.engine.paper_trading._is_market_open\", return_value=True),'
new = '    with (\n        patch(\"src.engine.paper_trading.datetime\", MockDateTime),\n        patch(\"src.engine.paper_trading._is_market_open\", return_value=True),'
content = content.replace(old, new)
open('tests/test_timeframe_strategy.py', 'w', encoding='utf-8').write(content)
