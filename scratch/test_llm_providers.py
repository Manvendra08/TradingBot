import os
import sys
import logging
from dotenv import load_dotenv

# Add workspace root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_llm")

from src.engine.llm_enrichment import _call_llm_api, LLMTradeVerdict

def test_providers():
    prompt = "Deliver a trade plan. Underlying is at 24500. Pick GO_LONG. PCR 1.2."
    
    # Save original keys
    keys = {
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY"),
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY"),
        "OPENCODE_API_KEY": os.environ.get("OPENCODE_API_KEY"),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    }
    
    # Clean keys
    for k in keys:
        if k in os.environ:
            del os.environ[k]
            
    # Helper to test a single provider in isolation
    def test_single_provider(provider_name, key_name, value):
        if not value:
            log.warning(f"{key_name} not configured in environment/.env, skipping {provider_name} test.")
            return
            
        log.info(f"\n--- Testing {provider_name} ---")
        os.environ[key_name] = value
        try:
            res = _call_llm_api("NIFTY", prompt, LLMTradeVerdict)
            if res:
                log.info(f"SUCCESS ({provider_name}): {res.action} | {res.instrument} | {res.confidence}% | thesis: {res.thesis}")
            else:
                log.error(f"FAILED ({provider_name}): Received None")
        except Exception as e:
            log.exception(f"{provider_name} failed with exception: {e}")
        finally:
            if key_name in os.environ:
                del os.environ[key_name]

    test_single_provider("Groq (with Gemma 2 9B as primary / other fallbacks)", "GROQ_API_KEY", keys["GROQ_API_KEY"])
    test_single_provider("OpenRouter (with Gemini 2.5 Flash Free as primary / other fallbacks)", "OPENROUTER_API_KEY", keys["OPENROUTER_API_KEY"])
    test_single_provider("OpenCode (with Gemini 2.5 Flash as primary / other fallbacks)", "OPENCODE_API_KEY", keys["OPENCODE_API_KEY"])
    test_single_provider("Gemini SDK (direct fallback)", "GEMINI_API_KEY", keys["GEMINI_API_KEY"])

    # Restore keys
    for k, v in keys.items():
        if v:
            os.environ[k] = v

if __name__ == "__main__":
    test_providers()
