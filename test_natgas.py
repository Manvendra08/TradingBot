from src.engine.intelligence import generate_intelligence
import json

alerts = [
    {'severity': 'HIGH', 'alert_type': 'OI_SPIKE', 'option_type': 'PE', 'strike': 9300, 'detail_json': json.dumps({'pct_change': 45.0})},
    {'severity': 'HIGH', 'alert_type': 'BUILDUP_CLASSIFY', 'option_type': 'CE', 'strike': 9300, 'detail_json': json.dumps({'buildup_type': 'Long Buildup'})},
    {'severity': 'HIGH', 'alert_type': 'OTM_UNUSUAL', 'option_type': 'PE', 'strike': 9200, 'detail_json': json.dumps({'pct_change': 50.0})},
]

chart = {
    'CRUDEOIL': {
        '1h': {'sentiment': 'BULLISH', 'ohlc': {'open': 9260.0, 'high': 9310.0, 'low': 9250.0, 'close': 9295.0}, 'atr_14': 5.0},
        '3h': {'sentiment': 'BULLISH', 'ohlc': {'open': 9230.0, 'high': 9340.0, 'low': 9200.0, 'close': 9295.0}, 'atr_14': 5.0},
    }
}

ctx = {
    'underlying': 9280.0,
    'price_change_pct': 0.2,
    'total_ce_oi': 84900,
    'total_pe_oi': 107600,
    'ce_oi_change': 0,
    'pe_oi_change': 0,
    'pcr': 1.40,
    'atm_strike': 9300,
    'support': 9200,
    'resistance': 9400,
    'max_pain': 9300,
    'chart_indicators': chart,
}

msg = generate_intelligence('CRUDEOIL', alerts, scan_context=ctx)
text = msg.telegram_text
with open('test_output.txt', 'w', encoding='utf-8') as f:
    f.write(text[:500])
    f.write('\n--- HAS Buy FUT ---\n')
    f.write(str('Buy FUT' in text))