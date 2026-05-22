"""
Unit tests for src/utils/formatting.py.
"""
import pytest
from src.utils.formatting import safe_num, fmt_oi, fmt_pct, fmt_int

def test_safe_num():
    assert safe_num(None) == 0.0
    assert safe_num("1,234.56") == 1234.56
    assert safe_num("—") == 0.0
    assert safe_num("abc") == 0.0
    assert safe_num(100) == 100.0

def test_fmt_oi():
    assert fmt_oi(25000000) == "2.50Cr"
    assert fmt_oi(350000) == "3.50L"
    assert fmt_oi(4500) == "4.5K"
    assert fmt_oi(250) == "250"
    assert fmt_oi(None) == "0"

def test_fmt_pct():
    assert fmt_pct(None) == "0.0%"
    assert fmt_pct(5.23) == "+5.2%"
    assert fmt_pct(-3.1) == "-3.1%"

def test_fmt_int():
    assert fmt_int(100.0) == "100"
    assert fmt_int("250") == "250"
    assert fmt_int(12.34) == "12.3"
