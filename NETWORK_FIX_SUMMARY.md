# Network Error Fixes - Summary

## Issues Addressed

### 1. SSL/Connection Errors with Groq API
**Problem**: Multiple SSL EOF and connection aborted errors when calling Groq API
```
SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in v...
ConnectionAbortedError(10053, 'An established connection was aborted by the software in your host machine'
```

**Root Cause**: 
- Network instability with keep-alive connections
- Too many retry attempts causing cascading failures
- No circuit breaker to prevent repeated failures

**Fixes Applied**:

#### A. Connection Handling (`llm_enrichment.py`)
- Added `Connection: close` header to prevent keep-alive issues
- Configured HTTP retry strategy with exponential backoff
- Reduced timeout from 15s to 12s for faster failure detection
- Reduced Groq model fallback list from 8 to 3 models to fail faster

#### B. Circuit Breaker Pattern
- Added circuit breaker with 3-failure threshold
- 5-minute cooldown when circuit opens
- Prevents cascading API failures during network issues
- Automatic reset on successful API call

#### C. Timeout Optimization
- Increased `get_llm_verdict` timeout from 20s to 30s
- Allows time for fallback models while preventing watchdog timeout
- Better balance between reliability and responsiveness

### 2. News Fetcher Connection Failures
**Problem**: Connection aborted when fetching news from TradingView
```
ConnectionAbortedError(10053, 'An established connection was aborted by the software in your host machine'
```

**Fixes Applied** (`news_fetcher.py`):
- Added retry strategy to all news fetcher functions:
  - `_fetch_tv_commodity_news()`
  - `_fetch_icici_commentary()`
  - `_fetch_way2wealth_commentary()`
- Configured HTTPAdapter with 2 retries and 0.5s backoff
- Added `Connection: close` header to prevent keep-alive issues
- Retry on status codes: 429, 500, 502, 503, 504

### 3. Pipeline Watchdog Timeout
**Problem**: Pipeline exceeding 300s timeout
```
ERROR | Watchdog: function 'run_all' timed out after 300s and might be hung
```

**Root Cause**: 
- LLM API calls trying too many fallback models
- Network errors causing retries that accumulate time
- No fast-fail mechanism

**Fixes Applied**:
- Circuit breaker prevents repeated API attempts during network issues
- Reduced model fallback list to fail faster
- Reduced per-model timeout from 15s to 12s
- Total LLM call timeout increased from 20s to 30s (but faster failure per attempt)

## Configuration Summary

### Retry Strategy
```python
retry_strategy = Retry(
    total=1,              # Reduced from 2 for faster failure
    backoff_factor=0.3,   # Quick backoff
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"]
)
```

### Circuit Breaker
```python
_CIRCUIT_BREAKER_THRESHOLD = 3        # Failures before opening
_CIRCUIT_BREAKER_COOLDOWN = 300.0     # 5 minutes
```

### Timeouts
- Per Groq model request: 12s (reduced from 15s)
- LLM verdict total: 30s (increased from 20s)
- Exit advice total: 15s (unchanged)
- News fetcher: 20s (unchanged)
- Pipeline watchdog: 300s (unchanged)

## Expected Behavior After Fixes

1. **Network Instability**: Circuit breaker activates after 3 consecutive failures, pausing LLM calls for 5 minutes
2. **Transient Errors**: Automatic retry with exponential backoff handles temporary issues
3. **Fast Failure**: Reduced model list and timeouts prevent long hangs
4. **Graceful Degradation**: Bot continues scanning without LLM enrichment when APIs fail

## Testing Recommendations

1. Monitor logs for circuit breaker activation:
   ```
   [llm] Circuit breaker ACTIVATED after 3 failures
   [llm] Circuit breaker OPEN for SYMBOL (cooldown ends in XXXs)
   ```

2. Verify successful failover:
   ```
   [llm] Groq PRIMARY → llama-3.3-70b-versatile
   [llm] LLMTradeVerdict OK via Groq llama-3.3-70b-versatile
   ```

3. Check for reduced timeout warnings:
   ```
   [llm] LLM API timed out after 30s for SYMBOL
   ```

4. Confirm news fetcher resilience:
   ```
   [news] Received response status 200 from TradingView for SYMBOL
   ```

## Rollback Instructions

If issues persist, revert by running:
```bash
git checkout HEAD~1 src/engine/llm_enrichment.py src/fetchers/news_fetcher.py
```

## Next Steps

1. Monitor scheduler logs for 24 hours
2. Check circuit breaker activation frequency
3. Verify LLM verdict success rate
4. Adjust circuit breaker threshold if needed (currently 3 failures)
