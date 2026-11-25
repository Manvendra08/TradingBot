from unittest.mock import patch

from src.engine import confidence_threshold as ct


def test_mcx_floor_falls_back_to_72_when_few_trades(tmp_path):
    ct._symbol_cache.clear()
    with patch.object(ct, "DERIVED_PATH", tmp_path / "derived_confidence.json"), \
         patch("src.engine.confidence_threshold._count_history", return_value=17), \
         patch("config.runtime_config.load_runtime_config", return_value={"derive_min_confidence": False}):
        assert ct.get_effective_mcx_confidence_floor("NATURALGAS") == 72


def test_mcx_floor_derives_per_symbol_when_enough_trades(tmp_path):
    ct._symbol_cache.clear()
    # 100 trades: 60-64 lose (50% wr at t=55/60 < 55% floor); >=65 slice wins 71.4% (+657 avg)
    history = [(62, -500.0)] * 30 + [(65, 1000.0)] * 50 + [(75, -200.0)] * 20
    with patch.object(ct, "DERIVED_PATH", tmp_path / "derived_confidence.json"), \
         patch("src.engine.confidence_threshold._count_history", return_value=len(history)), \
         patch("src.engine.confidence_threshold._load_history", return_value=history), \
         patch("config.runtime_config.load_runtime_config", return_value={"derive_min_confidence": True}):
        floor = ct.get_effective_mcx_confidence_floor("NATURALGAS")
    assert floor == 65
