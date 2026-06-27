/**
 * AI Insights Dashboard Module
 * AI_INTELLIGENCE_ROADMAP_v3.0 — Phase 4
 *
 * Self-contained module for the AI Intelligence dashboard panel.
 * Manages 4 panels with 4 states each: loading, ready, empty, error.
 *
 * Features:
 * - ML success probability gauge (HERO)
 * - Trade DNA historical match
 * - Edge health monitoring
 * - Top patterns display
 * - XSS-safe rendering via _esc()
 * - aria-live regions for screen readers
 * - Polling pauses when tab/panel not visible
 * - Humanized SHAP factor labels
 *
 * @module AIInsights
 */

const AIInsights = {
  // ── State ──────────────────────────────────────────────────────────────────
  symbol: null,
  verdict: null,
  confidence: 0,
  _filterSymbol: 'ALL',
  _pollTimer: null,
  _initialized: false,

  // ── Configuration ──────────────────────────────────────────────────────────
  POLL_INTERVAL_MS: 30000, // 30 seconds
  MAX_PATTERNS: 6,
  MAX_EDGE_STRATEGIES: 6,

  // ── Shared Helpers ─────────────────────────────────────────────────────────

  /**
   * XSS-safe HTML escaping.
   * Escapes &, <, >, " to prevent injection.
   */
  _esc(s) {
    return String(s ?? '').replace(/[&<>"]/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  },

  /**
   * Set panel data-state attribute for CSS state management.
   */
  _setState(el, state) {
    if (el) el.dataset.state = state;
  },

  /**
   * Show loading skeleton in a panel.
   */
  _skeleton(el, rows = 3, isHero = false) {
    if (!el) return;
    this._setState(el, 'loading');
    const skeletons = Array.from({ length: rows }, (_, i) => {
      const width = isHero ? '50%' : `${60 + Math.random() * 40}%`;
      const cls = isHero && i === 0 ? 'ai-skeleton ai-skeleton--hero' : 'ai-skeleton ai-skeleton--bar';
      return `<div class="${cls}" style="width:${width}"></div>`;
    }).join('');
    el.innerHTML = skeletons;
  },

  /**
   * Show empty state with message.
   */
  _empty(el, msg, icon = '○') {
    if (!el) return;
    this._setState(el, 'empty');
    el.innerHTML = `
      <div class="ai-state">
        <span class="ai-state__icon">${this._esc(icon)}</span>
        <p class="ai-state__text">${this._esc(msg)}</p>
      </div>`;
  },

  /**
   * Show error state with retry button.
   */
  _error(el, retryFn) {
    if (!el) return;
    this._setState(el, 'error');
    el.innerHTML = `
      <div class="ai-state">
        <span class="ai-state__icon ai-is-bad">⚠</span>
        <p class="ai-state__text">Couldn't load this panel.</p>
        <button class="ai-btn-ghost" type="button">Retry</button>
      </div>`;
    const btn = el.querySelector('button');
    if (btn && retryFn) {
      btn.addEventListener('click', retryFn);
    }
  },

  /**
   * Get semantic status from a value.
   * Returns {cls, label} for CSS class and accessible text.
   */
  _status(value, goodThreshold, warnThreshold) {
    if (value >= goodThreshold) return { cls: 'good', label: 'Strong' };
    if (value >= warnThreshold) return { cls: 'warn', label: 'Moderate' };
    return { cls: 'bad', label: 'Weak' };
  },

  /**
   * Map trend string to pill class and label.
   */
  _trendPill(trend) {
    const map = {
      'IMPROVING': ['good', 'Improving'],
      'DECLINING': ['bad', 'Declining'],
      'STABLE': ['neutral', 'Stable'],
      'INSUFFICIENT_HISTORY': ['neutral', 'New'],
    };
    const [cls, label] = map[trend] || ['neutral', trend || 'Unknown'];
    return `<span class="ai-pill ai-pill--${cls}">${this._esc(label)}</span>`;
  },

  /**
   * Humanize feature names for SHAP display.
   */
  _humanize(name) {
    const map = {
      'confidence': 'Confidence',
      'pcr': 'Put/Call ratio',
      'net_oi_change': 'OI bias (PE−CE)',
      'ce_oi_change': 'Call OI Δ',
      'pe_oi_change': 'Put OI Δ',
      'rsi_1h': 'RSI 1h',
      'rsi_3h': 'RSI 3h',
      'hour_of_day': 'Time of day',
      'day_of_week': 'Day of week',
      'days_to_expiry': 'Days to expiry',
      'distance_to_support_pct': 'Dist. to support',
      'distance_to_resistance_pct': 'Dist. to resistance',
      'distance_to_max_pain_pct': 'Dist. to max pain',
      'chart_conflict': 'Chart conflict',
      'price_change_pct': 'Price change %',
      'verdict_long_buildup': 'Long Buildup',
      'verdict_short_buildup': 'Short Buildup',
      'verdict_short_covering': 'Short Covering',
      'verdict_long_unwinding': 'Long Unwinding',
      'verdict_call_writing': 'Call Writing',
      'verdict_put_writing': 'Put Writing',
      'verdict_oi_bias_bullish': 'OI Bias Bullish',
      'verdict_oi_bias_bearish': 'OI Bias Bearish',
      'regime_trending': 'Trending regime',
      'regime_rangebound': 'Rangebound regime',
    };
    return map[name] || name.replace(/_/g, ' ').replace(/\bpct\b/g, '%');
  },

  /**
   * Fetch wrapper with error handling.
   */
  async _get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },

  // ── ML Prediction (HERO) ───────────────────────────────────────────────────

  async loadML() {
    const el = document.getElementById('ai-ml-content');
    if (!el) return;

    if (!this.symbol || !this.verdict) {
      return this._empty(el, 'Select a live signal to see ML prediction', '🤖');
    }

    this._skeleton(el, 4, true);

    try {
      const url = `/api/ai/ml-prediction/${encodeURIComponent(this.symbol)}`
        + `?verdict=${encodeURIComponent(this.verdict)}&confidence=${this.confidence}`;
      const d = await this._get(url);

      if (!d.available) {
        return this._empty(el, d.message || 'Model not trained yet (needs 30+ trades)', '🤖');
      }

      this._renderML(el, d);
    } catch (e) {
      console.error('[AI Insights] ML prediction failed:', e);
      this._error(el, () => this.loadML());
    }
  },

  _renderML(el, d) {
    this._setState(el, 'ready');

    const pct = (d.success_probability * 100);
    const s = this._status(d.success_probability, 0.6, 0.5);

    // Partial features warning
    const partial = d.context_complete === false
      ? `<span class="ai-pill ai-pill--warn" title="Scan features incomplete — using defaults">partial features</span>`
      : '';

    // SHAP factor bars
    const factors = (d.top_factors || []).map(([name, impact]) => {
      const width = Math.min(50, Math.abs(impact) * 50);
      const isPositive = impact >= 0;
      const barClass = isPositive ? 'ai-factor__bar-fill--pos ai-bg-good' : 'ai-factor__bar-fill--neg ai-bg-bad';
      const valueClass = isPositive ? 'ai-is-good' : 'ai-is-bad';

      return `
        <div class="ai-factor">
          <span class="ai-factor__label" title="${this._esc(name)}">${this._esc(this._humanize(name))}</span>
          <span class="ai-factor__bar">
            <span class="ai-factor__bar-fill ${barClass}" style="width:${width}%"></span>
          </span>
          <span class="ai-factor__value ${valueClass}">${isPositive ? '+' : ''}${impact.toFixed(2)}</span>
        </div>`;
    }).join('');

    el.innerHTML = `
      <div class="ai-gauge">
        <div class="ai-gauge__value ai-is-${s.cls}"
             role="status"
             aria-label="Success probability ${pct.toFixed(0)} percent, ${s.label}">
          ${pct.toFixed(0)}%
        </div>
        <div class="ai-gauge__label">
          <span class="ai-pill ai-pill--${s.cls}">${this._esc(s.label)}</span>
          <span class="ai-pill ai-pill--neutral">${this._esc(d.confidence_level)} confidence</span>
          ${partial}
        </div>
        <div class="ai-gauge__track">
          <div class="ai-gauge__fill ai-bg-${s.cls}" style="width:${pct}%"></div>
        </div>
      </div>
      <div style="margin-top:var(--ai-sp-4)">
        <div class="ai-micro" style="margin-bottom:var(--ai-sp-2)">Top drivers (SHAP)</div>
        ${factors || '<p class="ai-micro">No factor data available</p>'}
      </div>
      <p class="ai-micro" style="margin-top:var(--ai-sp-3); text-align:center;">
        Model v${this._esc(d.model_version)} · ${d.training_samples} training samples
      </p>`;
  },

  // ── Trade DNA ──────────────────────────────────────────────────────────────

  async loadDNA() {
    const el = document.getElementById('ai-dna-content');
    if (!el) return;

    if (!this.symbol || !this.verdict) {
      return this._empty(el, 'No active signal — select a symbol with a verdict', '🧬');
    }

    this._skeleton(el, 3);

    try {
      const url = `/api/ai/trade-dna/${encodeURIComponent(this.symbol)}`
        + `?verdict=${encodeURIComponent(this.verdict)}&confidence=${this.confidence}`;
      const d = await this._get(url);

      if (!d.match_found) {
        return this._empty(el, d.message || 'No similar historical trades found', '🧬');
      }

      this._renderDNA(el, d);
    } catch (e) {
      console.error('[AI Insights] Trade DNA failed:', e);
      this._error(el, () => this.loadDNA());
    }
  },

  _renderDNA(el, d) {
    this._setState(el, 'ready');

    const wr = d.historical_win_rate || 0;
    const s = this._status(wr, 0.6, 0.5);

    el.innerHTML = `
      <div class="ai-dna-hero">
        <span class="ai-dna-value ai-is-${s.cls}"
              role="status"
              aria-label="Historical win rate ${(wr * 100).toFixed(0)} percent">
          ${(wr * 100).toFixed(0)}%
        </span>
        <span class="ai-dna-label">historical win rate</span>
      </div>
      <div class="ai-dna-stats">
        <div class="ai-dna-stat">
          <span class="ai-dna-stat__value">${this._esc(d.similar_trades)}</span>
          <span class="ai-dna-stat__label">Similar trades</span>
        </div>
        <div class="ai-dna-stat">
          <span class="ai-dna-stat__value">₹${Math.round(d.avg_pnl || 0).toLocaleString()}</span>
          <span class="ai-dna-stat__label">Avg P&L</span>
        </div>
        <div class="ai-dna-stat">
          <span class="ai-dna-stat__value ai-is-good">₹${Math.round(d.avg_win || 0).toLocaleString()}</span>
          <span class="ai-dna-stat__label">Avg Win</span>
        </div>
        <div class="ai-dna-stat">
          <span class="ai-dna-stat__value ai-is-bad">₹${Math.round(d.avg_loss || 0).toLocaleString()}</span>
          <span class="ai-dna-stat__label">Avg Loss</span>
        </div>
      </div>
      <p class="ai-rec">${this._esc(d.confidence_note || '')}</p>`;
  },

  // ── Patterns ───────────────────────────────────────────────────────────────

  async loadPatterns() {
    const el = document.getElementById('ai-patterns-content');
    if (!el) return;

    this._skeleton(el, 4);

    try {
      const url = this._filterSymbol === 'ALL'
        ? '/api/ai/patterns'
        : `/api/ai/patterns?symbol=${encodeURIComponent(this._filterSymbol)}`;
      const list = await this._get(url);

      if (!list || !list.length) {
        return this._empty(el, 'No patterns yet — need 10+ trades per pattern', '📊');
      }

      this._setState(el, 'ready');
      const rows = list.slice(0, this.MAX_PATTERNS).map(p => {
        const s = this._status(p.win_rate, 0.6, 0.5);

        return `
          <div class="ai-row">
            <div class="ai-row__head">
              <span class="ai-row__name">${this._esc(p.name)}</span>
              <span class="ai-pill ai-pill--${s.cls}"
                    aria-label="${(p.win_rate * 100).toFixed(0)} percent win rate">
                ${(p.win_rate * 100).toFixed(0)}%
              </span>
            </div>
            <div class="ai-row__meta">
              <span>${this._esc(p.sample_size)} trades</span>
              <span>Avg ₹${Math.round(p.avg_pnl || 0).toLocaleString()}</span>
              ${p.best_time && p.best_time !== 'All day' ? `<span>Best: ${this._esc(p.best_time)}</span>` : ''}
            </div>
            <div class="ai-rec">${this._esc(p.recommendation || '')}</div>
          </div>`;
      }).join('');

      el.innerHTML = rows;
    } catch (e) {
      console.error('[AI Insights] Patterns failed:', e);
      this._error(el, () => this.loadPatterns());
    }
  },

  // ── Edge Health ────────────────────────────────────────────────────────────

  async loadEdge() {
    const el = document.getElementById('ai-edge-content');
    if (!el) return;

    this._skeleton(el, 3);

    try {
      const url = this._filterSymbol === 'ALL'
        ? '/api/ai/edge-health'
        : `/api/ai/edge-health/${encodeURIComponent(this._filterSymbol)}`;
      const list = await this._get(url);

      if (!list || !list.length) {
        return this._empty(el, 'No strategy data yet — need closed trades', '📉');
      }

      this._setState(el, 'ready');
      const rows = list.slice(0, this.MAX_EDGE_STRATEGIES).map(h => {
        const score = h.health_score || 0;
        const scoreClass = score >= 70 ? 'good' : score >= 50 ? 'warn' : 'bad';
        const winRate = h.current_win_rate || 0;

        return `
          <div class="ai-row">
            <div class="ai-row__head">
              <span class="ai-row__name">${this._esc(h.strategy_name)}</span>
              <span class="ai-pill ai-pill--${scoreClass}"
                    aria-label="Health score ${Math.round(score)} of 100">
                ${Math.round(score)}/100
              </span>
            </div>
            <div class="ai-row__meta">
              <span>Win ${(winRate * 100).toFixed(0)}%</span>
              ${this._trendPill(h.win_rate_trend)}
              ${this._trendPill(h.pnl_trend)}
            </div>
            <div class="ai-rec">${this._esc(h.recommendation || '')}</div>
          </div>`;
      }).join('');

      el.innerHTML = rows;
    } catch (e) {
      console.error('[AI Insights] Edge health failed:', e);
      this._error(el, () => this.loadEdge());
    }
  },

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Set the current signal context for ML and DNA panels.
   * Called when a live signal is detected.
   */
  setSignal({ symbol, verdict, confidence }) {
    this.symbol = symbol || null;
    this.verdict = verdict || null;
    this.confidence = confidence || 0;

    // Update symbol pill in header
    const pill = document.getElementById('ai-symbol-pill');
    if (pill) {
      pill.textContent = symbol || '—';
    }

    // Reload ML and DNA panels with new context
    this.loadML();
    this.loadDNA();
  },

  /**
   * Set the symbol filter for Patterns and Edge Health panels.
   * 'ALL' means no filter.
   */
  setFilterSymbol(symbol) {
    this._filterSymbol = symbol;
    this.loadPatterns();
    this.loadEdge();
  },

  /**
   * Refresh all panels.
   */
  refreshAll() {
    const updatedEl = document.getElementById('ai-updated');
    if (updatedEl) {
      updatedEl.textContent = 'Updated ' + new Date().toLocaleTimeString();
    }

    this.loadML();
    this.loadDNA();
    this.loadPatterns();
    this.loadEdge();
  },

  /**
   * Start polling.
   */
  _startPolling() {
    if (this._pollTimer) return; // Already polling
    this.refreshAll();
    this._pollTimer = setInterval(() => this.refreshAll(), this.POLL_INTERVAL_MS);
  },

  /**
   * Stop polling.
   */
  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  },

  /**
   * Initialize the AI Insights module.
   * Wires up event listeners and starts polling when visible.
   */
  init() {
    if (this._initialized) return;
    this._initialized = true;

    // Wire refresh button
    const refreshBtn = document.getElementById('ai-refresh-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this.refreshAll());
    }

    // Pause polling when tab/panel not visible (saves API calls)
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this._stopPolling();
      } else {
        const panel = document.querySelector('.ai-insights');
        if (panel && !panel.hidden) {
          this._startPolling();
        }
      }
    });

    // Initial load
    this._startPolling();
  },

  /**
   * Show the AI Insights panel (called when tab is activated).
   */
  show() {
    const panel = document.querySelector('.ai-insights');
    if (panel) {
      panel.hidden = false;
      this._startPolling();
    }
  },

  /**
   * Hide the AI Insights panel (called when tab is deactivated).
   */
  hide() {
    const panel = document.querySelector('.ai-insights');
    if (panel) {
      panel.hidden = true;
      this._stopPolling();
    }
  },
};

// ── Auto-initialize on DOM ready ─────────────────────────────────────────────
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => AIInsights.init());
} else {
  AIInsights.init();
}

// Export for use in other scripts
window.AIInsights = AIInsights;
