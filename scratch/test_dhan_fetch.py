import logging
from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher

logging.basicConfig(level=logging.INFO)
fetcher = DhanCommodityFetcher()
res = fetcher.fetch_option_chain("NATURALGAS")
if res:
    print("Underlying price:", res.get("underlying_price"))
    print("Expiry:", res.get("expiry"))
    print("Strikes count:", len(res.get("strikes", [])))
    if res.get("strikes"):
        print("First strike:", res["strikes"][0])
else:
    print("Failed to fetch")
