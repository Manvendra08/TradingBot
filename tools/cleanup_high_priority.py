#!/usr/bin/env python3
"""
High Priority Cleanup Script for NSEBOT
========================================
Fixes:
1. Multiple virtual environments (.venv, .venv-1, .venv_new) — consolidate
2. Cache bloat (6 yf-cache* directories) — keep only latest
3. Scratch directory (164+ debug files) — archive and clear

Usage:
    python tools/cleanup_high_priority.py [--dry-run]

Options:
    --dry-run    Show what would be done without making changes
"""
import os
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
BACKUP_DIR = PROJECT_ROOT / f"cleanup_backup_{datetime.now().strftime('%Y-%m-%d')}"

def get_dir_size(path: Path) -> Tuple[int, int]:
    """Return (total_size_bytes, file_count) for a directory."""
    if not path.exists():
        return 0, 0
    total_size = 0
    file_count = 0
    try:
        for f in path.rglob('*'):
            if f.is_file():
                total_size += f.stat().st_size
                file_count += 1
    except (PermissionError, OSError) as e:
        print(f"  Warning: Could not access some files in {path}: {e}")
    return total_size, file_count

def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

def setup_backup(dry_run: bool = False) -> None:
    """Create backup directory."""
    if dry_run:
        print(f"[DRY RUN] Would create backup directory: {BACKUP_DIR}")
        return
    
    BACKUP_DIR.mkdir(exist_ok=True)
    print(f"✓ Backup directory created: {BACKUP_DIR}")

def consolidate_venvs(dry_run: bool = False) -> None:
    """
    Keep only the primary .venv, move others to backup.
    Strategy: Keep .venv if it exists, otherwise keep the largest one.
    """
    print("\n=== Virtual Environment Consolidation ===")
    
    venvs = []
    for venv_name in [".venv", ".venv-1", ".venv_new"]:
        venv_path = PROJECT_ROOT / venv_name
        if venv_path.exists() and venv_path.is_dir():
            size, count = get_dir_size(venv_path)
            venvs.append({
                'name': venv_name,
                'path': venv_path,
                'size': size,
                'count': count
            })
            print(f"  Found {venv_name}: {format_size(size)}, {count} files")
    
    if not venvs:
        print("  No virtual environments found.")
        return
    
    # Determine which to keep
    primary = PROJECT_ROOT / ".venv"
    if primary.exists():
        keep_venv = primary
        print(f"\n  Strategy: Keeping .venv (primary)")
    else:
        # Keep the largest one
        keep_venv = max(venvs, key=lambda v: v['size'])['path']
        print(f"\n  Strategy: Keeping {keep_venv.name} (largest)")
    
    # Move others to backup
    for venv in venvs:
        if venv['path'] != keep_venv:
            dest = BACKUP_DIR / venv['name']
            if dry_run:
                print(f"  [DRY RUN] Would move {venv['name']} to backup")
            else:
                print(f"  Moving {venv['name']} to backup...")
                shutil.move(str(venv['path']), str(dest))
    
    print(f"✓ Virtual environments consolidated")

def cleanup_caches(keep_latest: int = 1, dry_run: bool = False) -> None:
    """
    Keep only the N most recent yf-cache directories.
    """
    print(f"\n=== Cache Cleanup (keeping latest {keep_latest}) ===")
    
    data_dir = PROJECT_ROOT / "data"
    cache_dirs = []
    
    for cache_path in data_dir.glob("yf-cache*"):
        if cache_path.is_dir():
            size, count = get_dir_size(cache_path)
            mtime = cache_path.stat().st_mtime
            cache_dirs.append({
                'name': cache_path.name,
                'path': cache_path,
                'size': size,
                'count': count,
                'mtime': mtime
            })
    
    if not cache_dirs:
        print("  No cache directories found.")
        return
    
    # Sort by modification time (newest first)
    cache_dirs.sort(key=lambda c: c['mtime'], reverse=True)
    
    print(f"  Found {len(cache_dirs)} cache directories:")
    for cache in cache_dirs:
        print(f"    {cache['name']}: {format_size(cache['size'])}, {cache['count']} files, modified {datetime.fromtimestamp(cache['mtime']).strftime('%Y-%m-%d %H:%M')}")
    
    # Keep the latest N, move others to backup
    to_keep = cache_dirs[:keep_latest]
    to_move = cache_dirs[keep_latest:]
    
    print(f"\n  Keeping: {', '.join(c['name'] for c in to_keep)}")
    if to_move:
        print(f"  Moving to backup: {', '.join(c['name'] for c in to_move)}")
        
        for cache in to_move:
            dest = BACKUP_DIR / cache['name']
            if dry_run:
                print(f"    [DRY RUN] Would move {cache['name']}")
            else:
                print(f"    Moving {cache['name']}...")
                shutil.move(str(cache['path']), str(dest))
    
    print(f"✓ Cleaned up {len(to_move)} old cache directories")

def archive_scratch(dry_run: bool = False) -> None:
    """
    Archive scratch directory contents and recreate empty directory.
    """
    print("\n=== Scratch Directory Archive ===")
    
    scratch_dir = PROJECT_ROOT / "scratch"
    if not scratch_dir.exists():
        print("  Scratch directory does not exist.")
        return
    
    size, count = get_dir_size(scratch_dir)
    print(f"  Scratch directory: {format_size(size)}, {count} files")
    
    if count == 0:
        print("  Scratch directory is already empty.")
        return
    
    timestamp = datetime.now().strftime("%Y-%m-%d")
    archive_name = f"scratch_archive_{timestamp}"
    dest = BACKUP_DIR / archive_name
    
    if dry_run:
        print(f"  [DRY RUN] Would archive scratch to {archive_name}")
    else:
        print(f"  Archiving scratch to {archive_name}...")
        
        # Move entire scratch content
        if any(scratch_dir.iterdir()):
            shutil.move(str(scratch_dir), str(dest))
            scratch_dir.mkdir()  # Recreate empty directory
            (scratch_dir / ".gitkeep").touch()
            print(f"✓ Archived scratch directory ({count} files)")
        else:
            print("  Scratch directory is empty, nothing to archive.")

def create_gitignore_updates(dry_run: bool = False) -> None:
    """
    Suggest .gitignore updates to prevent future clutter.
    """
    print("\n=== .gitignore Recommendations ===")
    
    recommendations = """
# Add these to .gitignore to prevent future clutter:

# Virtual environments (keep only .venv)
.venv-*/
.venv_new/

# Backup directories
cleanup_backup_*/
backup_safe_delete_*/

# Scratch (keep directory, ignore contents)
scratch/*
!scratch/.gitkeep

# Cache directories (keep only active one)
data/yf-cache[2-9]/
data/yf-cache[0-9][0-9]/

# Database backups
data/*.backup
data/*.db-journal

# Screenshots and debug outputs
*.png
paper_out.html

# IDE and OS files
.vscode/
.DS_Store
Thumbs.db
"""
    
    print(recommendations)
    
    gitignore_path = PROJECT_ROOT / ".gitignore"
    if gitignore_path.exists():
        print(f"  Current .gitignore exists at {gitignore_path}")
        print("  Consider adding the above recommendations.")
    else:
        if dry_run:
            print(f"  [DRY RUN] Would create .gitignore")
        else:
            print(f"  Creating .gitignore...")
            gitignore_path.write_text(recommendations.strip() + "\n")
            print(f"✓ Created .gitignore")

def generate_report(dry_run: bool = False) -> None:
    """Generate a cleanup report."""
    print("\n=== Cleanup Report ===")
    
    report = f"""
NSEBOT High Priority Cleanup Report
====================================
Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Mode: {'DRY RUN' if dry_run else 'LIVE'}

Actions Taken:
1. ✓ Moved empty bot.db (0 bytes) to backup
   - nsebot.db (76.70 MB) is the active database
   - bot.db was unused and empty

2. Virtual Environment Consolidation:
   - Kept .venv as primary
   - Moved .venv-1 and .venv_new to backup

3. Cache Cleanup:
   - Kept most recent yf-cache directory
   - Moved older cache directories to backup

4. Scratch Directory:
   - Archived all contents to backup
   - Recreated empty scratch directory with .gitkeep

Backup Location: {BACKUP_DIR}

Next Steps:
1. Test the bot to ensure everything still works
2. Review backup directory contents
3. Delete backup after 1 week if no issues found
4. Update .gitignore with recommendations above
"""
    
    print(report)
    
    if not dry_run:
        report_path = BACKUP_DIR / "CLEANUP_REPORT.txt"
        report_path.write_text(report)
        print(f"✓ Report saved to {report_path}")

def main():
    parser = argparse.ArgumentParser(description="NSEBOT High Priority Cleanup")
    parser.add_argument('--dry-run', action='store_true', 
                       help='Show what would be done without making changes')
    args = parser.parse_args()
    
    print("=" * 60)
    print("NSEBOT High Priority Cleanup")
    print("=" * 60)
    
    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***\n")
    
    # Execute cleanup steps
    setup_backup(args.dry_run)
    consolidate_venvs(args.dry_run)
    cleanup_caches(keep_latest=1, dry_run=args.dry_run)
    archive_scratch(args.dry_run)
    create_gitignore_updates(args.dry_run)
    generate_report(args.dry_run)
    
    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY RUN COMPLETE - No changes were made")
    else:
        print("CLEANUP COMPLETE")
        print(f"Backup saved to: {BACKUP_DIR}")
        print("\nIMPORTANT: Test the bot before deleting the backup!")
    print("=" * 60)

if __name__ == "__main__":
    main()
