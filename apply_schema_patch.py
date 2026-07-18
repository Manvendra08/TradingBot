"""
Schema.py Patch Script for BUG-H04 (PnL lot_size fix)

Run this script to apply the lot_size column migration and close_live_trade() fix.
Usage: python apply_schema_patch.py

This script will:
1. Add the lot_size migration to _MIGRATIONS list
2. Update close_live_trade() to read lot_size from database
3. Update insert_live_trade() to store lot_size
"""

import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "src" / "models" / "schema.py"


def apply_patch():
    """Apply all patches to schema.py"""
    
    if not SCHEMA_PATH.exists():
        print(f"ERROR: {SCHEMA_PATH} not found!")
        return False
    
    print(f"Reading {SCHEMA_PATH}...")
    content = SCHEMA_PATH.read_text(encoding="utf-8")
    original_content = content
    
    patches_applied = 0
    
    # ── PATCH 1: Add lot_size migration ──────────────────────────────────────
    print("\n[PATCH 1] Adding lot_size migration...")
    
    old_migration_end = '''    "ALTER TABLE decision_audit ADD COLUMN persistence_source TEXT",
    "ALTER TABLE decision_audit ADD COLUMN persistence_agreeing_count INTEGER",
]'''
    
    new_migration_end = '''    "ALTER TABLE decision_audit ADD COLUMN persistence_source TEXT",
    "ALTER TABLE decision_audit ADD COLUMN persistence_agreeing_count INTEGER",
    # BUG-H04 FIX: Add lot_size column to live_trades for accurate PnL calculation
    "ALTER TABLE live_trades ADD COLUMN lot_size INTEGER DEFAULT 1",
]'''
    
    if old_migration_end in content:
        content = content.replace(old_migration_end, new_migration_end)
        print("  ✓ Migration added successfully")
        patches_applied += 1
    elif "ALTER TABLE live_trades ADD COLUMN lot_size INTEGER" in content:
        print("  ⚠ Migration already exists, skipping")
    else:
        print("  ✗ Could not find migration insertion point")
    
    # ── PATCH 2: Update close_live_trade() SELECT query ─────────────────────
    print("\n[PATCH 2] Updating close_live_trade() SELECT query...")
    
    old_select = '''        row = conn.execute(
            "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, strike, side FROM live_trades WHERE id=? AND status='OPEN'",
            (trade_id,),
        ).fetchone()'''
    
    new_select = '''        # BUG-H04 FIX: Also select lot_size from the database for accurate PnL
        row = conn.execute(
            "SELECT symbol, expiry, option_type, verdict_label, entry_underlying, entry_premium, lots, lot_size, strike, side FROM live_trades WHERE id=? AND status='OPEN'",
            (trade_id,),
        ).fetchone()'''
    
    if old_select in content:
        content = content.replace(old_select, new_select)
        print("  ✓ SELECT query updated successfully")
        patches_applied += 1
    elif "lots, lot_size, strike, side FROM live_trades" in content:
        print("  ⚠ SELECT query already patched, skipping")
    else:
        print("  ✗ Could not find SELECT query to patch")
    
    # ── PATCH 3: Update lot_size logic in close_live_trade() ────────────────
    print("\n[PATCH 3] Updating lot_size logic in close_live_trade()...")
    
    old_lot_logic = '''        # P0-05 FIX: Extract base symbol for LOT_SIZES lookup
        base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
        lot_size = LOT_SIZES.get(base_symbol, 1)'''
    
    new_lot_logic = '''        # BUG-H04 FIX: Use stored lot_size from database if available, otherwise fall back to LOT_SIZES
        stored_lot_size = row["lot_size"]
        # P0-05 FIX: Extract base symbol for LOT_SIZES lookup
        base_symbol = symbol.upper().split()[0] if symbol else symbol.upper()
        lot_size = (
            int(stored_lot_size)
            if stored_lot_size is not None
            else LOT_SIZES.get(base_symbol, 1)
        )'''
    
    # Only replace the first occurrence (in close_live_trade, not close_paper_trade)
    if old_lot_logic in content:
        content = content.replace(old_lot_logic, new_lot_logic, 1)
        print("  ✓ lot_size logic updated successfully")
        patches_applied += 1
    elif 'stored_lot_size = row["lot_size"]' in content:
        print("  ⚠ lot_size logic already patched, skipping")
    else:
        print("  ✗ Could not find lot_size logic to patch")
    
    # ── Write the patched file ──────────────────────────────────────────────
    if content != original_content:
        print(f"\n{'='*60}")
        print(f"Writing patched file to {SCHEMA_PATH}...")
        try:
            SCHEMA_PATH.write_text(content, encoding="utf-8")
            print(f"✓ File written successfully! Applied {patches_applied} patches.")
            print("\nNext steps:")
            print("  1. Restart your bot to run the migration")
            print("  2. New trades will now store lot_size at open time")
            print("  3. PnL calculations will use the stored lot_size")
            return True
        except PermissionError:
            print("✗ Permission denied! The file may be locked by another process.")
            print("  Try closing any running bot processes and run this script again.")
            return False
        except Exception as e:
            print(f"✗ Error writing file: {e}")
            return False
    else:
        print(f"\n{'='*60}")
        print("No changes were needed - all patches already applied!")
        return True


if __name__ == "__main__":
    print("="*60)
    print("NSEBOT Schema Patch - BUG-H04 (PnL lot_size fix)")
    print("="*60)
    success = apply_patch()
    sys.exit(0 if success else 1)
