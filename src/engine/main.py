import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.engine.llm_enrichment import get_strategy_optimization_advice, LLMStrategyOptimization
from src.models.schema import get_closed_trades
from config.runtime_config import load_runtime_config, save_runtime_config

log = logging.getLogger("nsebot.api")

app = FastAPI(
    title="NSEBOT AI API",
    description="API for AI-driven strategy optimization and configuration management.",
    version="1.0.0",
)

class ConfigUpdatePayload(BaseModel):
    changes: dict[str, float | str | int]

@app.get("/api/ai/review_strategy", response_model=LLMStrategyOptimization)
async def review_strategy():
    """
    Triggers the AI to review past trade history and suggest strategy optimizations.
    """
    log.info("Received request to review strategy.")
    try:
        # Fetch last 50 closed paper trades for review
        trades = get_closed_trades(limit=50)
        if not trades:
            raise HTTPException(status_code=404, detail="No closed trades found for review.")

        optimization_advice = get_strategy_optimization_advice(trades)

        if not optimization_advice:
            raise HTTPException(status_code=500, detail="AI failed to generate optimization advice.")

        log.info("AI strategy review completed successfully.")
        return optimization_advice
    except HTTPException as e:
        raise e
    except Exception as e:
        log.exception("Error during AI strategy review:")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

_CONFIG_SCHEMA: dict[str, tuple] = {
    # key: (type, min, max) or (type, allowed_values)
    "live_min_confidence_core":       (int,   40,  95),
    "live_max_concurrent_positions":  (int,    1,   5),
    "live_ai_decision_mode":          (str,  {"advisory", "boost_only", "full"}),
    "live_ai_min_confidence_boost":   (int,   60, 100),
    "live_ai_min_confidence_veto":    (int,   70, 100),
    "live_capital_per_trade_inr":     (float, 5000, 500000),
}

def _validate_config_changes(changes: dict) -> dict:
    """Strip unknown keys; clamp/cast known ones. Raises ValueError on type failure."""
    validated = {}
    for k, v in changes.items():
        if k not in _CONFIG_SCHEMA:
            log.warning("Config update: unknown key '%s' rejected", k)
            continue
        schema = _CONFIG_SCHEMA[k]
        typ = schema[0]
        try:
            cast_v = typ(v)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Config key '{k}': cannot cast {v!r} to {typ}: {e}")
        if isinstance(schema[1], set):          # allowed string values
            if cast_v not in schema[1]:
                raise ValueError(f"Config key '{k}': value '{cast_v}' not in {schema[1]}")
        else:                                    # numeric range
            lo, hi = schema[1], schema[2]
            if not (lo <= cast_v <= hi):
                raise ValueError(f"Config key '{k}': {cast_v} outside [{lo}, {hi}]")
        validated[k] = cast_v
    return validated

@app.post("/api/config/update")
async def update_bot_config(payload: ConfigUpdatePayload):
    """
    Applies suggested configuration changes to the bot's runtime config.
    """
    log.info("Received request to update bot config with changes: %s", payload.changes)
    try:
        validated = _validate_config_changes(payload.changes)
        save_runtime_config(validated)
        log.info("Bot configuration updated successfully.")
        return {"message": "Configuration updated successfully."}
    except ValueError as e:
        log.warning("Config validation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("Error applying bot configuration update:")
        raise HTTPException(status_code=500, detail=f"Failed to update configuration: {e}")

@app.get("/api/config/current")
async def get_current_config():
    """
    Retrieves the current bot's runtime configuration.
    """
    try:
        config = load_runtime_config()
        return config
    except Exception as e:
        log.exception("Error fetching current config:")
        raise HTTPException(status_code=500, detail=f"Failed to fetch current configuration: {e}")


if __name__ == "__main__":
    import uvicorn
    # This is for local testing. In production, you'd run this via a WSGI server like Gunicorn.
    # Example: uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
    # Ensure your main bot process can access this API, or integrate it directly.
    uvicorn.run(app, host="0.0.0.0", port=8000)