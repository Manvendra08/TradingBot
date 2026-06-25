"""
ScrapeGraphAI-powered fetcher — AI-based extraction from NSE web pages.
Used as a resilient fallback when JSON APIs fail.
"""
import logging
from src.fetchers.base_fetcher import BaseFetcher
from scrapegraphai.graphs import SmartScraperGraph

log = logging.getLogger(__name__)

# ScrapeGraphAI config — uses local LLM (Ollama) by default for cost-free operation.
# To use OpenAI/GPT-4, set OPENAI_API_KEY in environment and adjust model string.
SG_CONFIG = {
    "llm": {
        "model": "ollama/llama3.2",  # requires: ollama run llama3.2
        # Alternative: "model": "openai/gpt-4o",  # requires OPENAI_API_KEY
        "temperature": 0,
    },
    "embeddings": {
        "model": "ollama/nomic-embed-text",
    },
    "headless": True,
    "verbose": False,
}


class ScrapeGraphFetcher(BaseFetcher):
    name = "scrapegraph"

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        """
        Extract option chain from NSE option-chain page using ScrapeGraphAI.
        Returns normalised dict or None on failure.
        """
        url = f"https://www.nseindia.com/option-chain?symbol={symbol}"
        prompt = f"""
        Extract the NSE option chain data for {symbol} from the page.
        Return a JSON object with exactly these fields:
        - underlying_price: (number) current spot price of the underlying
        - expiry: (string) nearest expiry date in YYYY-MM-DD format
        - strikes: (array) array of strike objects, each with:
            * strike: (number) strike price
            * option_type: (string) either "CE" or "PE"
            * ltp: (number) last traded price
            * oi: (integer) open interest
            * oi_change: (integer) change in open interest
            * volume: (integer) traded volume
            * iv: (number) implied volatility (as percentage, e.g. 18.5 for 18.5%)
            * bid: (number) bid price
            * ask: (number) ask price
        If a field is not available, use 0 for numbers, empty string for expiry.
        Only return valid JSON, no markdown, no explanations.
        """

        try:
            graph = SmartScraperGraph(prompt=prompt, source=url, config=SG_CONFIG)
            result = graph.run()
            if not result:
                log.warning("[scrapegraph] returned empty result for %s", symbol)
                return None
            return self._normalise(symbol, result)
        except Exception as exc:
            log.error("[scrapegraph] extraction failed for %s: %s", symbol, exc)
            return None

    def _normalise(self, symbol: str, raw: dict) -> dict | None:
        """
        Convert ScrapeGraphAI output to NSEBOT's standard format.
        Validates and ensures all required fields are present with correct types.
        """
        try:
            # Extract and validate underlying price
            underlying_price = float(raw.get("underlying_price", 0) or 0)

            # Extract and validate expiry (ensure YYYY-MM-DD)
            expiry_raw = raw.get("expiry", "")
            expiry = ""
            if isinstance(expiry_raw, str) and len(expiry_raw) == 10:
                # Basic format check: YYYY-MM-DD
                parts = expiry_raw.split("-")
                if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
                    expiry = expiry_raw

            # Extract and validate strikes array
            strikes_input = raw.get("strikes", [])
            if not isinstance(strikes_input, list):
                strikes_input = []

            strikes = []
            for strike_obj in strikes_input:
                if not isinstance(strike_obj, dict):
                    continue
                try:
                    strike = {
                        "strike": float(strike_obj.get("strike", 0) or 0),
                        "option_type": str(strike_obj.get("option_type", "")).upper(),
                        "ltp": float(strike_obj.get("ltp", 0) or 0),
                        "oi": int(strike_obj.get("oi", 0) or 0),
                        "oi_change": int(strike_obj.get("oi_change", 0) or 0),
                        "volume": int(strike_obj.get("volume", 0) or 0),
                        "iv": float(strike_obj.get("iv", 0) or 0),
                        "bid": float(strike_obj.get("bid", 0) or 0),
                        "ask": float(strike_obj.get("ask", 0) or 0),
                    }
                    # Only include if option_type is valid
                    if strike["option_type"] in ("CE", "PE"):
                        strikes.append(strike)
                except (ValueError, TypeError):
                    # Skip malformed strike objects
                    continue

            # Return None if no valid strikes found
            if not strikes:
                log.warning("[scrapegraph] no valid strikes extracted for %s", symbol)
                return None

            return {
                "symbol": symbol,
                "underlying_price": underlying_price,
                "expiry": expiry,
                "strikes": strikes,
            }
        except Exception as exc:
            log.error("[scrapegraph] normalisation failed for %s: %s", symbol, exc)
            return None