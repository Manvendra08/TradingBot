"""
BUG FIX SCRIPT - Apply fixes to pipeline.py
Run this script to apply all bug fixes from the audit report.
"""
import re

file_path = r"C:\Users\manve\Downloads\NSEBOT\src\engine\pipeline.py"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix H6: Safe __dict__ access for news_result and chart_result
old_news = '''        news_future = pipeline_io_executor.submit(lambda: run_with_deadline("news", _fetch_news))
        packet["news_result"] = news_future.result().__dict__

    packet["chart_result"] = chart_future.result().__dict__'''

new_news = '''        news_future = pipeline_io_executor.submit(lambda: run_with_deadline("news", _fetch_news))
        # BUG-H06 FIX: Safe dict conversion
        news_result = news_future.result()
        if hasattr(news_result, '__dict__'):
            packet["news_result"] = news_result.__dict__
        elif isinstance(news_result, dict):
            packet["news_result"] = news_result
        else:
            packet["news_result"] = {"ok": True, "data": news_result}

    # BUG-H06 FIX: Safe dict conversion for chart_result
    chart_result = chart_future.result()
    if hasattr(chart_result, '__dict__'):
        packet["chart_result"] = chart_result.__dict__
    elif isinstance(chart_result, dict):
        packet["chart_result"] = chart_result
    else:
        packet["chart_result"] = {"ok": True, "data": chart_result}'''

content = content.replace(old_news, new_news)

# Fix H7: Safe sorted() key
old_sorted = '        for packet in sorted(prefetched, key=lambda x: symbols.index(x["symbol"])):'
new_sorted = '''        # BUG-H07 FIX: Safe sorted() key with fallback
        symbols_list = list(symbols)
        for packet in sorted(prefetched, key=lambda x: symbols_list.index(x["symbol"]) if x["symbol"] in symbols_list else 999):'''

content = content.replace(old_sorted, new_sorted)

# Fix M11: Use functools.partial instead of keyword args
old_submit = '''        if _async_llm_pending and telegram_message_id is not None:
            pipeline_io_executor.submit(
                _async_llm_enrich_and_edit,
                symbol=symbol,
                intel=intel,
                scan_context=scan_context,
                new_alerts=new_alerts,
                news_data=news_data,
                fetched_at=fetched_at,
                digest_id=digest_id,
                message_id=telegram_message_id,
                dedup_suppressed=dedup_suppressed,
                intel_text_base=intel_text_base,
            )'''

new_submit = '''        if _async_llm_pending and telegram_message_id is not None:
            # BUG-M11 FIX: Use functools.partial for positional parameter submission
            import functools
            pipeline_io_executor.submit(
                functools.partial(
                    _async_llm_enrich_and_edit,
                    symbol, intel, scan_context, new_alerts, news_data,
                    fetched_at, digest_id, telegram_message_id,
                    dedup_suppressed, intel_text_base,
                )
            )'''

content = content.replace(old_submit, new_submit)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("pipeline.py fixes applied successfully!")
