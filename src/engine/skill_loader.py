"""
Skill Loader Utility for NSEBOT.

Dynamically loads and extracts operational guidelines from installed global
skills (~/.gemini/config/skills/) to enrich LLM prompts with specialized
trading, investment research, and adversarial risk disciplines.
Includes in-memory caching and built-in fallbacks.
"""

from __future__ import annotations

import functools
import logging
import os
import pathlib
import re

log = logging.getLogger("nsebot.skill_loader")

# Default global skills directory
DEFAULT_SKILLS_DIR = pathlib.Path.home() / ".gemini" / "config" / "skills"


def _clean_markdown_for_prompt(text: str, max_chars: int = 1500) -> str:
    """Strip YAML frontmatter, headers, links, and truncate cleanly."""
    if not text:
        return ""

    # Strip YAML frontmatter if present
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]

    # Clean markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Normalize excessive newlines and whitespace
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > max_chars:
        cutoff = text[:max_chars].rfind("\n")
        if cutoff > max_chars // 2:
            text = text[:cutoff] + "\n..."
        else:
            text = text[:max_chars] + "..."

    return text.strip()


@functools.lru_cache(maxsize=32)
def get_skill_guidelines(
    skill_name: str,
    max_chars: int = 1500,
    skills_dir: str | None = None,
) -> str:
    """Read operational guidelines from an installed skill markdown file.

    Results are cached in memory. If the skill file is missing, returns empty string.
    """
    base_dir = pathlib.Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
    skill_file = base_dir / skill_name / "SKILL.md"

    if not skill_file.is_file():
        log.debug("Skill file not found at %s", skill_file)
        return ""

    try:
        content = skill_file.read_text(encoding="utf-8", errors="ignore")
        return _clean_markdown_for_prompt(content, max_chars=max_chars)
    except Exception as exc:
        log.warning("Failed to read skill %s from %s: %s", skill_name, skill_file, exc)
        return ""


@functools.lru_cache(maxsize=1)
def get_reality_check_guardrails() -> str:
    """Extract adversarial risk and reality-checking guardrails for trade entry.

    Falls back to embedded production rules if external skill is unavailable.
    """
    skill_text = get_skill_guidelines("agency-reality-checker", max_chars=1200)

    fallback_rules = """• ADVERSARIAL REALITY CHECK:
  - Default to NO_TRADE on ambiguous or conflicting signals. A missed trade costs ₹0; a bad trade destroys capital.
  - Risk-to-Reward must be mathematically ≥ 1:2 based on hard S/R levels.
  - Reject long premium entries within 0.5% of major Call OI resistance or into expected IV crush events.
  - Require explicit invalidation level (stop loss). If invalidation cannot be cleanly defined from data, DO NOT TRADE."""

    if not skill_text:
        return fallback_rules

    return f"""• ADVERSARIAL REALITY CHECK (agency-reality-checker):
  - Enforce ruthless skepticism on trade justification. Reject confirmation bias.
  - Math must reconcile: (target - entry) / (entry - stop_loss) ≥ 1:1.5.
  - High confidence (>75%) requires multi-factor alignment: OI flow + 3H chart breakout + clean premium pricing.
  - If evidence is mixed or edge is marginal, verdict MUST be NO_TRADE."""


@functools.lru_cache(maxsize=1)
def get_investment_research_guidance() -> str:
    """Extract investment research and macro discounting guidance for sentiment weighting.

    Falls back to embedded production rules if external skill is unavailable.
    """
    skill_text = get_skill_guidelines("agency-investment-researcher", max_chars=1200)

    fallback_rules = """• VARIANT PERCEPTION & MACRO NEWS:
  - Distinguish between priced-in consensus and genuine news catalysts.
  - Headlines older than 2 hours without fresh price momentum should be treated as neutral noise.
  - FII/DII flow and heavy institutional block positioning override retail option excitement."""

    if not skill_text:
        return fallback_rules

    return f"""• VARIANT PERCEPTION & MACRO (agency-investment-researcher):
  - Do not merely follow sentiment consensus; identify where market expectation diverges from price action.
  - Institutional order flow and OI buildup always take precedence over retail news sentiment.
  - Disregard repetitive or recycled media headlines that lack immediate volume confirmation."""


@functools.lru_cache(maxsize=1)
def get_autopsy_analyst_guidance() -> str:
    """Extract post-mortem analytical framework for closed trade autopsies.

    Falls back to embedded production rules if external skill is unavailable.
    """
    skill_text = get_skill_guidelines("agency-financial-analyst", max_chars=1200)

    fallback_rules = """• FINANCIAL POST-MORTEM FRAMEWORK:
  - Classify trade outcome: Was it an execution failure, thesis invalidation, or normal statistical loss?
  - Differentiate between bad luck (exogenous gap/shock) and bad process (chasing into resistance, late entry).
  - Quantify whether the stop-loss was respected or if slippage/theta decay degraded the edge."""

    if not skill_text:
        return fallback_rules

    return f"""• FINANCIAL POST-MORTEM FRAMEWORK (agency-financial-analyst):
  - Objectively isolate whether the loss was structural (engine thesis failure) vs tactical (theta decay / slippage).
  - Assess if trade was opened too close to an invalidation level or held past its logical expiry.
  - Note actionable adjustments to strike selection or confidence threshold."""
