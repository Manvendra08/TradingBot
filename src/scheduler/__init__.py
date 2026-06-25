"""
Scheduler Package
AI_INTELLIGENCE_ROADMAP_v3.0

Modules:
  job_runner       - Main scheduler loop (scan + periodic tasks)
  ml_training_job  - ML model retraining (Phase 2)
    - Event-driven: 20+ trades since last train
    - Event-driven: edge health < 60 (wired from pipeline Phase 3)
    - Weekly fallback: Sunday 2 AM IST
"""
