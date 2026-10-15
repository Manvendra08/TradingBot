import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.engine.accounting import (
    calculate_leg_pnl,
    calculate_transaction_costs,
    calculate_book_pnl,
)

def test_calculate_leg_pnl_base():
    """Verify basic BUY and SELL calculations for profit, loss, and breakeven."""
    # BUY long: profit
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=120.0, side='BUY', lots=1, lot_size=25) == 500.0
    # BUY long: loss
    assert calculate_leg_pnl(entry_premium=120.0, current_or_exit_premium=100.0, side='BUY', lots=1, lot_size=25) == -500.0
    # BUY long: breakeven
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=100.0, side='BUY', lots=2, lot_size=25) == 0.0

    # SELL short: profit
    assert calculate_leg_pnl(entry_premium=120.0, current_or_exit_premium=100.0, side='SELL', lots=1, lot_size=25) == 500.0
    # SELL short: loss
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=120.0, side='SELL', lots=1, lot_size=25) == -500.0
    # SELL short: breakeven
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=100.0, side='SELL', lots=2, lot_size=25) == 0.0


def test_calculate_leg_pnl_zero_ltp_and_edge_cases():
    """Verify edge cases: zero LTP (worthless expiration), zero lots, zero lot sizes, and fractions."""
    # Zero LTP: BUY long expires worthless -> full premium loss
    assert calculate_leg_pnl(entry_premium=150.0, current_or_exit_premium=0.0, side='BUY', lots=2, lot_size=25) == -7500.0
    # Zero LTP: SELL short expires worthless -> full premium profit
    assert calculate_leg_pnl(entry_premium=150.0, current_or_exit_premium=0.0, side='SELL', lots=2, lot_size=25) == 7500.0

    # Zero lots -> 0 P&L
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=150.0, side='BUY', lots=0, lot_size=25) == 0.0
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=50.0, side='SELL', lots=0, lot_size=25) == 0.0

    # Zero lot size -> 0 P&L
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=150.0, side='BUY', lots=2, lot_size=0) == 0.0

    # Fractional premiums and precision
    assert calculate_leg_pnl(entry_premium=100.55, current_or_exit_premium=102.30, side='BUY', lots=2, lot_size=30) == round((102.30 - 100.55) * 2 * 30, 2)
    assert calculate_leg_pnl(entry_premium=105.75, current_or_exit_premium=102.25, side='SELL', lots=1, lot_size=50) == round((105.75 - 102.25) * 1 * 50, 2)

    # String normalization and whitespace
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=120.0, side=' buy ', lots=1, lot_size=25) == 500.0
    assert calculate_leg_pnl(entry_premium=120.0, current_or_exit_premium=100.0, side=' sell ', lots=1, lot_size=25) == 500.0
    assert calculate_leg_pnl(entry_premium=100.0, current_or_exit_premium=90.0, side='', lots=1, lot_size=10) == 100.0


def test_calculate_transaction_costs_options():
    """Verify options transaction costs for Index (NIFTY) and Stock (RELIANCE)."""
    # 1. Index Option (NIFTY) SELL side
    cost_nifty_sell = calculate_transaction_costs(
        option_type='CE',
        side='SELL',
        entry_premium=200.0,
        entry_underlying=0.0,
        exit_premium=210.0,
        exit_underlying=0.0,
        lot_size=25,
        lots=2,
        symbol='NIFTY',
    )
    entry_to = 200.0 * 25 * 2   # 10,000
    exit_to = 210.0 * 25 * 2    # 10,500
    tot_to = entry_to + exit_to # 20,500
    sell_to = entry_to
    buy_to = exit_to
    stt = tot_to * 0.000625     # 12.8125
    exch = tot_to * 0.0005      # 10.25
    brokerage = 40.0
    sebi = tot_to * 0.000001    # 0.0205
    stamp = buy_to * 0.00003    # 0.315
    gst = (brokerage + exch + sebi) * 0.18 # 9.04869
    expected_nifty = round(brokerage + stt + exch + sebi + stamp + gst, 2)
    assert cost_nifty_sell == expected_nifty

    # 2. Stock Option (RELIANCE) BUY side
    cost_rel_buy = calculate_transaction_costs(
        option_type='PE',
        side='BUY',
        entry_premium=1000.0,
        entry_underlying=0.0,
        exit_premium=950.0,
        exit_underlying=0.0,
        lot_size=50,
        lots=1,
        symbol='RELIANCE',
    )
    entry_to = 1000.0 * 50 * 1  # 50,000
    exit_to = 950.0 * 50 * 1   # 47,500
    tot_to = entry_to + exit_to # 97,500
    sell_to = exit_to
    buy_to = entry_to
    stt = sell_to * 0.000625    # 29.6875
    exch = tot_to * 0.0005      # 48.75
    brokerage = 40.0
    sebi = tot_to * 0.000001    # 0.0975
    stamp = buy_to * 0.00003    # 1.5
    gst = (brokerage + exch + sebi) * 0.18 # 15.99255
    expected_rel = round(brokerage + stt + exch + sebi + stamp + gst, 2)
    assert cost_rel_buy == expected_rel


def test_calculate_transaction_costs_futures_and_mcx():
    """Verify futures transaction costs (Index, Stock, MCX) and MCX commodity options."""
    # 1. Futures NIFTY SELL side
    cost_fut_sell = calculate_transaction_costs(
        option_type='FUT',
        side='SELL',
        entry_premium=0.0,
        entry_underlying=22000.0,
        exit_premium=0.0,
        exit_underlying=22100.0,
        lot_size=25,
        lots=2,
        symbol='NIFTY',
    )
    entry_to = 22000.0 * 25 * 2 # 1,100,000
    exit_to = 22100.0 * 25 * 2  # 1,105,000
    tot_to = entry_to + exit_to # 2,205,000
    sell_to = entry_to
    buy_to = exit_to
    stt = sell_to * 0.000125    # 137.5
    exch = tot_to * 0.000019   # 41.895
    brokerage = 40.0
    sebi = tot_to * 0.000001    # 2.205
    stamp = buy_to * 0.00002    # 22.1
    gst = (brokerage + exch + sebi) * 0.18 # 15.138
    expected_fut = round(brokerage + stt + exch + sebi + stamp + gst, 2)
    assert cost_fut_sell == expected_fut

    # 2. MCX Commodity Option (NATURALGAS)
    cost_mcx_opt = calculate_transaction_costs(
        option_type='CE',
        side='SELL',
        entry_premium=500.0,
        entry_underlying=0.0,
        exit_premium=520.0,
        exit_underlying=0.0,
        lot_size=250,
        lots=1,
        symbol='NATURALGAS',
    )
    entry_to = 500.0 * 250 * 1  # 125,000
    exit_to = 520.0 * 250 * 1   # 130,000
    tot_to = entry_to + exit_to # 255,000
    sell_to = entry_to          # 125,000
    buy_to = exit_to           # 130,000
    stt = sell_to * 0.0005      # 62.5
    exch = tot_to * 0.0005      # 127.5
    brokerage = 40.0
    sebi = tot_to * 0.000001    # 0.255
    stamp = buy_to * 0.00003    # 3.9
    gst = (brokerage + exch + sebi) * 0.18 # 30.1959
    expected_mcx_opt = round(brokerage + stt + exch + sebi + stamp + gst, 2)
    assert cost_mcx_opt == expected_mcx_opt

    # 3. MCX Commodity Futures (CRUDEOIL) SELL side
    cost_mcx_fut = calculate_transaction_costs(
        option_type='FUT',
        side='SELL',
        entry_premium=0.0,
        entry_underlying=6000.0,
        exit_premium=0.0,
        exit_underlying=5900.0,
        lot_size=100,
        lots=1,
        symbol='CRUDEOIL',
    )
    entry_to = 6000.0 * 100 * 1 # 600,000
    exit_to = 5900.0 * 100 * 1  # 590,000
    tot_to = entry_to + exit_to # 1,190,000
    sell_to = entry_to          # 600,000
    buy_to = exit_to           # 590,000
    stt = sell_to * 0.0001      # 60.0 (CTT)
    exch = tot_to * 0.000019   # 22.61
    brokerage = 40.0
    sebi = tot_to * 0.000001    # 1.19
    stamp = buy_to * 0.00002    # 11.8
    gst = (brokerage + exch + sebi) * 0.18 # 11.484
    expected_mcx_fut = round(brokerage + stt + exch + sebi + stamp + gst, 2)
    assert cost_mcx_fut == expected_mcx_fut


def test_calculate_book_pnl_single_and_multi_leg():
    """Verify single-leg and multi-leg open and closed positions."""
    # Single leg open BUY
    legs = [{'side': 'BUY', 'option_type': 'CE', 'entry_premium': 100.0, 'lots': 1, 'status': 'OPEN', 'cmp': 110.0}]
    total, enriched = calculate_book_pnl(legs, lot_size=25, symbol='NIFTY')
    assert total == 250.0
    assert enriched[0]['pnl'] == 250.0
    assert enriched[0]['cmp'] == 110.0

    # Single leg closed SELL
    legs2 = [{'side': 'SELL', 'option_type': 'PE', 'entry_premium': 120.0, 'exit_premium': 100.0, 'lots': 2, 'status': 'CLOSED'}]
    total2, enriched2 = calculate_book_pnl(legs2, lot_size=30, symbol='BANKNIFTY')
    assert total2 == 1200.0
    assert enriched2[0]['pnl'] == 1200.0

    # Mixed open and closed
    legs3 = [
        {'side': 'BUY', 'option_type': 'CE', 'entry_premium': 100.0, 'lots': 1, 'status': 'OPEN', 'cmp': 90.0},
        {'side': 'SELL', 'option_type': 'PE', 'entry_premium': 120.0, 'exit_premium': 130.0, 'lots': 2, 'status': 'CLOSED'},
    ]
    total3, enriched3 = calculate_book_pnl(legs3, lot_size=25, symbol='NIFTY')
    # leg1: (90 - 100) * 1 * 25 = -250
    # leg2: (120 - 130) * 2 * 25 = -500
    assert total3 == -750.0
    assert enriched3[0]['pnl'] == -250.0
    assert enriched3[1]['pnl'] == -500.0


def test_calculate_book_pnl_zero_ltp():
    """Verify zero LTP handling: expired worthless positions must register full loss/profit."""
    # BUY call expires worthless (cmp=0.0) -> full loss of 100 pts * 25 = -2500
    legs_buy_zero = [{'side': 'BUY', 'entry_premium': 100.0, 'cmp': 0.0, 'lots': 1, 'status': 'OPEN'}]
    total_buy, enriched_buy = calculate_book_pnl(legs_buy_zero, lot_size=25, symbol='NIFTY')
    assert total_buy == -2500.0
    assert enriched_buy[0]['pnl'] == -2500.0
    assert enriched_buy[0]['cmp'] == 0.0

    # SELL put expires worthless (cmp=0.0) -> full gain of 80 pts * 25 = +2000
    legs_sell_zero = [{'side': 'SELL', 'entry_premium': 80.0, 'cmp': 0.0, 'lots': 1, 'status': 'OPEN'}]
    total_sell, enriched_sell = calculate_book_pnl(legs_sell_zero, lot_size=25, symbol='NIFTY')
    assert total_sell == 2000.0
    assert enriched_sell[0]['pnl'] == 2000.0
    assert enriched_sell[0]['cmp'] == 0.0

    # CLOSED leg settled at 0.0
    legs_closed_zero = [{'side': 'SELL', 'entry_premium': 60.0, 'exit_premium': 0.0, 'lots': 2, 'status': 'CLOSED'}]
    total_cz, enriched_cz = calculate_book_pnl(legs_closed_zero, lot_size=25, symbol='NIFTY')
    assert total_cz == 3000.0  # 60 * 2 * 25
    assert enriched_cz[0]['pnl'] == 3000.0


def test_calculate_book_pnl_rollover():
    """Verify rollover: near-month leg closed with locked P&L, far-month opened with live P&L."""
    rollover_book = [
        # Near-month closed leg: sold at 150, closed at 30 -> +120 profit
        {
            'symbol': 'NIFTY',
            'option_type': 'CE',
            'side': 'SELL',
            'entry_premium': 150.0,
            'exit_premium': 30.0,
            'lots': 2,
            'status': 'CLOSED',
        },
        # Far-month new leg: sold at 160, current cmp 140 -> +20 unrealized profit
        {
            'symbol': 'NIFTY',
            'option_type': 'CE',
            'side': 'SELL',
            'entry_premium': 160.0,
            'cmp': 140.0,
            'lots': 2,
            'status': 'OPEN',
        },
    ]
    total_pnl, enriched = calculate_book_pnl(rollover_book, lot_size=25, symbol='NIFTY')
    # Leg 1: (150 - 30) * 2 * 25 = 6000.0
    # Leg 2: (160 - 140) * 2 * 25 = 1000.0
    assert total_pnl == 7000.0
    assert enriched[0]['pnl'] == 6000.0
    assert enriched[1]['pnl'] == 1000.0


def test_calculate_book_pnl_strategies():
    """Verify standard derivative multi-leg structures: Short Straddle and Iron Condor."""
    # 1. Short Straddle: Sell 22000 CE @ 120, Sell 22000 PE @ 110. CMP: CE @ 90, PE @ 140
    straddle = [
        {'side': 'SELL', 'option_type': 'CE', 'entry_premium': 120.0, 'cmp': 90.0, 'lots': 1, 'status': 'OPEN'},
        {'side': 'SELL', 'option_type': 'PE', 'entry_premium': 110.0, 'cmp': 140.0, 'lots': 1, 'status': 'OPEN'},
    ]
    tot_straddle, _ = calculate_book_pnl(straddle, lot_size=25, symbol='NIFTY')
    # CE PnL: (120 - 90) * 25 = +750
    # PE PnL: (110 - 140) * 25 = -750
    # Net: 0.0
    assert tot_straddle == 0.0

    # 2. Iron Condor (4 legs)
    iron_condor = [
        # Bull Put wing: Sell 21800 PE @ 60, Buy 21600 PE @ 20
        {'side': 'SELL', 'option_type': 'PE', 'entry_premium': 60.0, 'cmp': 10.0, 'lots': 1, 'status': 'OPEN'},
        {'side': 'BUY', 'option_type': 'PE', 'entry_premium': 20.0, 'cmp': 5.0, 'lots': 1, 'status': 'OPEN'},
        # Bear Call wing: Sell 22200 CE @ 50, Buy 22400 CE @ 15
        {'side': 'SELL', 'option_type': 'CE', 'entry_premium': 50.0, 'cmp': 8.0, 'lots': 1, 'status': 'OPEN'},
        {'side': 'BUY', 'option_type': 'CE', 'entry_premium': 15.0, 'cmp': 2.0, 'lots': 1, 'status': 'OPEN'},
    ]
    tot_ic, enriched_ic = calculate_book_pnl(iron_condor, lot_size=25, symbol='NIFTY')
    # Leg 1: (60 - 10) * 25 = +1250
    # Leg 2: (5 - 20) * 25 = -375
    # Leg 3: (50 - 8) * 25 = +1050
    # Leg 4: (2 - 15) * 25 = -325
    # Total = 1250 - 375 + 1050 - 325 = 1600.0
    assert tot_ic == 1600.0
    assert [leg['pnl'] for leg in enriched_ic] == [1250.0, -375.0, 1050.0, -325.0]


def test_calculate_book_pnl_immutability():
    """Verify that calculate_book_pnl is a pure function that does not mutate caller's inputs."""
    original_leg = {'side': 'BUY', 'option_type': 'CE', 'entry_premium': 100.0, 'cmp': 110.0, 'status': 'OPEN'}
    original_legs_list = [original_leg]

    total, enriched = calculate_book_pnl(original_legs_list, lot_size=25, symbol='NIFTY')
    assert 'pnl' not in original_leg
    assert 'pnl' in enriched[0]


def test_calculate_book_pnl_premium_fallbacks_and_costs():
    """Verify current_premium and entry_premium fallback paths for OPEN/CLOSED legs and transaction deduction."""
    # Stock futures costs (stock STT rate 0.02%)
    c_stock_fut = calculate_transaction_costs(
        option_type='FUT', side='SELL', entry_premium=0, entry_underlying=1000,
        exit_premium=0, exit_underlying=1050, lot_size=100, lots=1, symbol='TCS'
    )
    assert c_stock_fut == 74.14

    # Closed leg with current_premium fallback
    legs_cl_cp = [{'side': 'SELL', 'entry_premium': 100, 'current_premium': 80, 'status': 'CLOSED'}]
    pnl_cl_cp, _ = calculate_book_pnl(legs_cl_cp, lot_size=10)
    assert pnl_cl_cp == 200.0

    # Closed leg with entry_premium fallback
    legs_cl_fb = [{'side': 'SELL', 'entry_premium': 100, 'status': 'CLOSED'}]
    pnl_cl_fb, _ = calculate_book_pnl(legs_cl_fb, lot_size=10)
    assert pnl_cl_fb == 0.0

    # Open leg with current_premium fallback
    legs_op_cp = [{'side': 'BUY', 'entry_premium': 100, 'current_premium': 120, 'status': 'OPEN'}]
    pnl_op_cp, _ = calculate_book_pnl(legs_op_cp, lot_size=10)
    assert pnl_op_cp == 200.0

    # Open leg with entry_premium fallback
    legs_op_fb = [{'side': 'BUY', 'entry_premium': 100, 'status': 'OPEN'}]
    pnl_op_fb, _ = calculate_book_pnl(legs_op_fb, lot_size=10)
    assert pnl_op_fb == 0.0

    # Deduct transaction costs
    legs_tx = [{'side': 'BUY', 'option_type': 'CE', 'entry_premium': 100, 'cmp': 110, 'status': 'OPEN'}]
    net_pnl, _ = calculate_book_pnl(legs_tx, lot_size=25, symbol='NIFTY', deduct_transaction_costs=True)
    assert net_pnl == 196.34


if __name__ == '__main__':
    test_calculate_leg_pnl_base()
    test_calculate_leg_pnl_zero_ltp_and_edge_cases()
    test_calculate_transaction_costs_options()
    test_calculate_transaction_costs_futures_and_mcx()
    test_calculate_book_pnl_single_and_multi_leg()
    test_calculate_book_pnl_zero_ltp()
    test_calculate_book_pnl_rollover()
    test_calculate_book_pnl_strategies()
    test_calculate_book_pnl_immutability()
    test_calculate_book_pnl_premium_fallbacks_and_costs()
    print('All comprehensive accounting test cases passed successfully!')