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

@app.post("/api/config/update")
async def update_bot_config(payload: ConfigUpdatePayload):
    """
    Applies suggested configuration changes to the bot's runtime config.
    """
    log.info("Received request to update bot config with changes: %s", payload.changes)
    try:
        save_runtime_config(payload.changes)
        log.info("Bot configuration updated successfully.")
        return {"message": "Configuration updated successfully."}
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