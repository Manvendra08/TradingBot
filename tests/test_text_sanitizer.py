"""
Unit tests for Text Sanitizer & Mojibake Repair Utility (src/utils/text_sanitizer.py).
Verifies repairing junk characters (e.g. 'CE Î”+14,999' -> 'CE Δ+14,999', '1.05â†’0.81' -> '1.05→0.81').
"""
import unittest
from src.utils.text_sanitizer import sanitize_mojibake


class TestTextSanitizer(unittest.TestCase):

    def test_junk_character_repairs(self):
        # Exact string reported in user prompt
        user_junk_text = (
            "Fresh call writing (CE Î”+14,999) with "
            "accelerating short buildup (+41,235 "
            "net OI over 5 scans) confirms bearish "
            "positioning as price rejects R=265 "
            "into Pain=270. PCR falling 1.05â†’0.81 "
            "signals building bearish skew ahead of "
            "Thursday EIA."
        )
        cleaned = sanitize_mojibake(user_junk_text)
        self.assertIn("CE Δ+14,999", cleaned)
        self.assertIn("1.05→0.81", cleaned)

    def test_additional_mojibake_symbols(self):
        self.assertEqual(sanitize_mojibake("â‚¹500"), "₹500")
        self.assertEqual(sanitize_mojibake("âœ… OK"), "✅ OK")
        self.assertEqual(sanitize_mojibake("â†‘ Up"), "↑ Up")
        self.assertEqual(sanitize_mojibake("â†“ Down"), "↓ Down")
        self.assertEqual(sanitize_mojibake("â€” dash"), "— dash")

    def test_user_log_mojibake_repairs(self):
        log_sample_1 = (
            "book ML-NAT-89efc5a1 — AI recommends ADJUST: CE tested (delta 0.44, "
            "premium â‚¹11.4 vs entry â‚¹9.7); loss on call leg inflates max loss. "
            "Roll CE 275 to 285 narrows exposure, recovers â‚¹800 credit, reduces delta to 0.35."
        )
        cleaned_1 = sanitize_mojibake(log_sample_1)
        self.assertIn("premium ₹11.4 vs entry ₹9.7", cleaned_1)
        self.assertIn("recovers ₹800 credit", cleaned_1)

        log_sample_2 = (
            "book ML-NAT-6389dd43 — AI recommends ADJUST: CE tested (Î”=0.50, P&L â‚¹-30000 loss). "
            "Roll 270 CE up to 280 CE: capture â‚¹4k credit against loss, reduce delta to 0.40. "
            "PE still profitable (Î”=-0.38, +â‚¹25625)."
        )
        cleaned_2 = sanitize_mojibake(log_sample_2)
        self.assertIn("Δ=0.50", cleaned_2)
        self.assertIn("₹-30000", cleaned_2)
        self.assertIn("Δ=-0.38", cleaned_2)
        self.assertIn("+₹25625", cleaned_2)

    def test_clean_text_unmodified(self):
        clean = "CE Δ+15,000 | PCR 1.05 → 0.81 | Target ₹265 ✅"
        self.assertEqual(sanitize_mojibake(clean), clean)

    def test_none_and_empty(self):
        self.assertEqual(sanitize_mojibake(""), "")
        self.assertEqual(sanitize_mojibake(None), "")


if __name__ == "__main__":
    unittest.main()
