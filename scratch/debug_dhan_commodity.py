import logging
import json
import urllib.request
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher
from src.utils.dhan_resolver import get_dhan_security_id

logging.basicConfig(level=logging.DEBUG)
fetcher = DhanCommodityFetcher()
secid = get_dhan_security_id("NATURALGAS")
print("resolved secid:", secid)

# 1. Fetch builtup live price
live_builtup = fetcher._fetch_builtup_live_price(secid)
print("live builtup:", live_builtup)

# 2. Fetch page HTML
url = fetcher.URL_TEMPLATE.format(slug="natural-gas") if hasattr(fetcher, "URL_TEMPLATE") else "https://dhan.co/commodity/natural-gas-option-chain/"
html = fetcher._fetch_html(url)
print("HTML fetched successfully:", bool(html))

if html:
    from src.fetchers.dhan_commodity_fetcher import (
        _clean_text, _strip_tags, _extract_next_data, _extract_page_props,
        _pick_scrip_id, _pick_option_expj, _extract_underlying_from_page_props,
        _extract_underlying
    )
    page_text = _clean_text(_strip_tags(html))
    next_data = _extract_next_data(html)
    page_props = _extract_page_props(next_data)
    sid = _pick_scrip_id(page_props)
    expj = _pick_option_expj(page_props)
    print("HTML props - sid:", sid, "expj:", expj)
    
    underlying_props = _extract_underlying_from_page_props(page_props, "NATURALGAS")
    print("underlying from props:", underlying_props)
    
    underlying_text = _extract_underlying(page_text, "NATURALGAS")
    print("underlying from page text:", underlying_text)
    
    # 3. Fetch scanx option chain
    if sid and expj:
        raw_oc = fetcher._fetch_scanx_option_chain(sid, expj)
        print("scanx sltp:", (raw_oc or {}).get("data", {}).get("sltp"))

# Run full option chain fetch
oc = fetcher.fetch_option_chain("NATURALGAS")
print("Full fetch option chain underlying:", oc.get("underlying_price") if oc else None)
print("Full fetch option chain expiry:", oc.get("expiry") if oc else None)
