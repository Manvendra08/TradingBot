import os
import sys
import logging
from dotenv import load_dotenv

# Add workspace root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_llm")

from src.engine.llm_enrichment import _call_alternative_llm, LLMTradeVerdict

def test_providers():
    prompt = "Analyze market context and provide trade verdict: NIFTY underlying is at 23500, Rule engine verdict is Long Buildup, confidence is 80%."
    
    # 1. Test Groq
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        log.info(f"Testing Groq API with key prefix: {groq_key[:10]}...")
        old_or_key = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            res = _call_alternative_llm(prompt, LLMTradeVerdict)
            if res:
                log.info(f"SUCCESS (Groq): {res}")
            else:
                log.error("FAILED (Groq): Received None")
        finally:
            if old_or_key:
                os.environ["OPENROUTER_API_KEY"] = old_or_key
    else:
        log.warning("GROQ_API_KEY not found in environment.")

    # 2. Test OpenRouter
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if or_key:
        log.info(f"Testing OpenRouter API with key prefix: {or_key[:10]}...")
        old_groq_key = os.environ.pop("GROQ_API_KEY", None)
        try:
            res = _call_alternative_llm(prompt, LLMTradeVerdict)
            if res:
                log.info(f"SUCCESS (OpenRouter): {res}")
            else:
                log.error("FAILED (OpenRouter): Received None")
        finally:
            if old_groq_key:
                os.environ["GROQ_API_KEY"] = old_groq_key
    else:
        log.warning("OPENROUTER_API_KEY not found in environment.")

if __name__ == "__main__":
    test_providers()
