"""
Tests for Cost-Aware LLM Pipeline (SKILL.md)
Validates model routing by task complexity, immutable cost tracking, narrow retries,
prompt caching, budget enforcement, and pipeline composition.
"""

import unittest
from src.engine.llm_enrichment import (
    CostRecord,
    CostTracker,
    BudgetExceededError,
    estimate_call_cost,
    record_call_cost,
    get_cost_tracker,
    select_model_by_complexity,
    call_with_narrow_retry,
    build_cached_messages,
    process_cost_aware_pipeline,
)


class TestCostAwareLLMPipeline(unittest.TestCase):

    def test_model_routing_by_complexity(self):
        # Simple task -> Haiku
        m_simple = select_model_by_complexity(text_length=500, item_count=5)
        self.assertIn("haiku", m_simple)

        # Complex text -> Sonnet
        m_complex_text = select_model_by_complexity(text_length=10_000, item_count=5)
        self.assertIn("sonnet", m_complex_text)

        # High item count -> Sonnet
        m_complex_items = select_model_by_complexity(text_length=500, item_count=35)
        self.assertIn("sonnet", m_complex_items)

        # Force model override
        m_forced = select_model_by_complexity(text_length=100, force_model="custom-model")
        self.assertEqual(m_forced, "custom-model")

    def test_immutable_cost_tracker(self):
        t0 = CostTracker(budget_limit=0.10)
        self.assertEqual(t0.total_cost, 0.0)
        self.assertFalse(t0.over_budget)

        rec1 = CostRecord(model="claude-haiku", input_tokens=1000, output_tokens=200, cost_usd=0.0016)
        t1 = t0.add(rec1)

        # Verify immutability
        self.assertEqual(len(t0.records), 0)
        self.assertEqual(len(t1.records), 1)
        self.assertAlmostEqual(t1.total_cost, 0.0016)
        self.assertFalse(t1.over_budget)

        # Over budget check
        rec2 = CostRecord(model="claude-sonnet", input_tokens=50000, output_tokens=10000, cost_usd=0.25)
        t2 = t1.add(rec2)
        self.assertTrue(t2.over_budget)

    def test_pricing_estimation(self):
        cost_haiku = estimate_call_cost("claude-haiku", 1_000_000, 1_000_000)
        self.assertEqual(cost_haiku, 4.80)  # 0.80 + 4.00

        cost_sonnet = estimate_call_cost("claude-sonnet", 1_000_000, 1_000_000)
        self.assertEqual(cost_sonnet, 18.00)  # 3.00 + 15.00

    def test_narrow_retry_transient_vs_permanent(self):
        # Transient error retries then succeeds
        attempts = 0

        def transient_func():
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                class RateLimitErr(Exception):
                    status_code = 429
                raise RateLimitErr("Rate limit exceeded")
            return "SUCCESS"

        result = call_with_narrow_retry(transient_func, max_retries=3, initial_delay=0.01)
        self.assertEqual(result, "SUCCESS")
        self.assertEqual(attempts, 2)

        # Permanent error fails fast without retry
        perm_attempts = 0

        def permanent_func():
            nonlocal perm_attempts
            perm_attempts += 1
            class AuthErr(Exception):
                status_code = 401
            raise AuthErr("Unauthorized API Key")

        with self.assertRaises(Exception):
            call_with_narrow_retry(permanent_func, max_retries=3, initial_delay=0.01)
        self.assertEqual(perm_attempts, 1)

    def test_prompt_caching(self):
        sys_long = "System instruction " * 100
        msgs = build_cached_messages(sys_long, "User query")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"][0]["cache_control"], {"type": "ephemeral"})

        sys_short = "Short prompt"
        msgs_short = build_cached_messages(sys_short, "User query")
        self.assertEqual(msgs_short[0]["content"], "Short prompt")

    def test_process_cost_aware_pipeline(self):
        tracker = CostTracker(budget_limit=1.00)
        res, new_tracker = process_cost_aware_pipeline(
            text="Scan data query",
            system_prompt="System prompt " * 100,
            tracker=tracker,
            item_count=5,
        )
        self.assertEqual(res["status"], "ok")
        self.assertGreater(new_tracker.total_cost, 0.0)

        # Test BudgetExceededError
        tight_tracker = CostTracker(budget_limit=0.000001, records=(
            CostRecord(model="claude-sonnet", input_tokens=1000, output_tokens=1000, cost_usd=0.05),
        ))
        with self.assertRaises(BudgetExceededError):
            process_cost_aware_pipeline(
                text="Scan data query",
                system_prompt="System prompt",
                tracker=tight_tracker,
            )


if __name__ == "__main__":
    unittest.main()
