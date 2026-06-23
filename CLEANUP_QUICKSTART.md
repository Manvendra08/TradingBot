# NSEBOT Cleanup - Quick Start Guide

## ✅ Completed Actions

### 1. Database Consolidation ✅
- **Moved:** `data/bot.db` (0 bytes, empty) → `cleanup_backup_2026-06-23/bot.db.empty`
- **Active DB:** `data/nsebot.db` (76.70 MB) — confirmed in `config/settings.py`
- **Status:** ✅ COMPLETE

### 2. Documentation Created ✅
- **Created:** `docs/CLEANUP_REPORT.md` — Comprehensive cleanup documentation
- **Created:** `tools/cleanup_high_priority.py` — Python cleanup script
- **Created:** `tools/cleanup_high_priority.bat` — Windows batch script
- **Updated:** `.gitignore` — Added entries to prevent future clutter
- **Created:** `scratch/.gitkeep` — Preserves scratch directory in git
- **Status:** ✅ COMPLETE

---

## ⚠️ Requires Manual Execution

### 3. Virtual Environment Consolidation
**Current State:**
```
.venv       (primary - keep this)
.venv-1     (move to backup)
.venv_new   (move to backup)
```

**Execute:**
```powershell
# Option A: Run batch script (easiest)
tools\cleanup_high_priority.bat

# Option B: Manual commands
move .venv-1 cleanup_backup_2026-06-23\
move .venv_new cleanup_backup_2026-06-23\
```

**Verify:**
```powershell
dir .venv* /AD
# Should show only: .venv
```

---

### 4. Cache Directory Cleanup
**Current State:**
```
data/yf-cache   (move to backup)
data/yf-cache2  (move to backup)
data/yf-cache3  (move to backup)
data/yf-cache4  (move to backup)
data/yf-cache5  (move to backup)
data/yf-cache6  (keep - most recent)
```

**Execute:**
```powershell
# Option A: Run batch script (easiest)
tools\cleanup_high_priority.bat

# Option B: Manual commands
cd data
move yf-cache ..\cleanup_backup_2026-06-23\
move yf-cache2 ..\cleanup_backup_2026-06-23\
move yf-cache3 ..\cleanup_backup_2026-06-23\
move yf-cache4 ..\cleanup_backup_2026-06-23\
move yf-cache5 ..\cleanup_backup_2026-06-23\
cd ..
```

**Verify:**
```powershell
dir data\yf-cache* /AD
# Should show only: yf-cache6
```

---

### 5. Scratch Directory Archive
**Current State:**
```
scratch/  (164+ debug files - archive and clear)
```

**Execute:**
```powershell
# Option A: Run batch script (easiest)
tools\cleanup_high_priority.bat

# Option B: Manual commands
move scratch cleanup_backup_2026-06-23\scratch_archive_2026-06-23
mkdir scratch
echo. > scratch\.gitkeep
```

**Verify:**
```powershell
dir scratch
# Should show only: .gitkeep
```

---

## 🚀 Recommended Execution Flow

### Step 1: Review Documentation
```powershell
# Read the comprehensive cleanup report
notepad docs\CLEANUP_REPORT.md
```

### Step 2: Run Cleanup Script
```powershell
# Preview what will be done (no changes)
python tools\cleanup_high_priority.py --dry-run

# Execute cleanup
python tools\cleanup_high_priority.py

# OR use the batch script (Windows)
tools\cleanup_high_priority.bat
```

### Step 3: Verify Cleanup
```powershell
# Check database
dir data\*.db

# Check virtual environments
dir .venv* /AD

# Check cache directories
dir data\yf-cache* /AD

# Check scratch directory
dir scratch
```

### Step 4: Test Bot
```powershell
# Run test suite
python -m pytest tests/ -v

# OR start the bot
python main.py
```

### Step 5: Wait & Delete Backup
```powershell
# Wait 1 week, then delete backup
rmdir /s /q cleanup_backup_2026-06-23
```

---

## 📊 Expected Results

### Before Cleanup
- ❌ 2 database files (1 empty)
- ❌ 3 virtual environments
- ❌ 6 cache directories
- ❌ 164+ scratch files
- ❌ Cluttered .gitignore

### After Cleanup
- ✅ 1 database file (nsebot.db)
- ✅ 1 virtual environment (.venv)
- ✅ 1 cache directory (yf-cache6)
- ✅ Empty scratch directory
- ✅ Clean .gitignore

### Disk Space Savings
- **Virtual environments:** ~200-1000 MB
- **Cache directories:** ~50-200 MB
- **Scratch directory:** ~10-50 MB
- **Total:** ~260-1250 MB freed

---

## 🛡️ Safety Measures

1. **Backup First:** All moved items go to `cleanup_backup_2026-06-23/`
2. **Dry Run:** Python script supports `--dry-run` to preview changes
3. **Verification:** Post-cleanup checks ensure bot still works
4. **Rollback:** 1-week waiting period before permanent deletion
5. **Documentation:** Full cleanup report in `docs/CLEANUP_REPORT.md`

---

## 📚 Related Documentation

- **Full Cleanup Report:** `docs/CLEANUP_REPORT.md`
- **Architecture:** `docs/architecture.md`
- **Trading Strategy:** `docs/TRADING_STRATEGY.md`
- **AGoT Playbook:** `docs/AGoT-playbook.md`

---

## ❓ Troubleshooting

**Issue:** Script fails with "Access denied"  
**Solution:** Close Python processes/IDE, run as Administrator

**Issue:** Bot fails after cleanup  
**Solution:** Check active venv, restore from backup if needed

**Issue:** Cache errors  
**Solution:** Bot will regenerate cache automatically

**Issue:** Missing scratch files  
**Solution:** Restore from `cleanup_backup_2026-06-23/scratch_archive_*`

---

**Status:** Ready for Manual Execution  
**Date:** 2026-06-23  
**Estimated Time:** 5-10 minutes
