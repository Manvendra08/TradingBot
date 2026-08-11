export const meta = {
  name: 'review-shoonya-ip-guard',
  description: 'Adversarially review the Shoonya IP-change guard diff across correctness, integration, and edge cases',
  phases: [
    { title: 'Review' },
    { title: 'Verify' },
  ],
}

const FEATURE = `Feature requirement: On the FIRST Shoonya fetch attempt of each IST day, the bot must check whether the public IP has changed before doing any Shoonya work. If it changed, alert the user once (Telegram) and skip Shoonya for the rest of the day, moving to fallback fetchers. Must be fail-open (never block Shoonya when IP cannot be determined).`

const FILES = `
Files changed/added (read them in full before judging):
- src/fetchers/shoonya_ip_guard.py (NEW — the guard: run_daily_ip_check, shoonya_should_skip, state in data/shoonya_ip_state.json)
- src/utils/ip_monitor.py (added optional timeout/max_providers/retries params to _fetch_public_ip)
- src/fetchers/router.py (_try_fetcher short-circuits "shoonya" when shoonya_should_skip())
- src/fetchers/shoonya_fetcher.py (ShoonyaFetcher.login() returns False when shoonya_should_skip())
- tests/test_shoonya_ip_guard.py
- .gitignore (added data/shoonya_ip_state.json)
- data/sentinel/KNOWLEDGE_BASE.md (added F129)
Also read for context: src/engine/pipeline.py lines 62-71 (_refresh_ip_async already calls ip_monitor.check_ip_changed every scan), src/scheduler/job_runner.py daily reauth, src/alerts/telegram_dispatcher.py send_text.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          file: { type: 'string' },
          summary: { type: 'string', description: 'One-sentence claim of the defect' },
          detail: { type: 'string', description: 'Concrete failure scenario: inputs/state -> wrong behavior. Include line references.' },
          suggestion: { type: 'string' },
        },
        required: ['severity', 'file', 'summary', 'detail'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean', description: 'true if the defect is real and matters, false if refuted/harmless' },
    reasoning: { type: 'string' },
  },
  required: ['isReal', 'reasoning'],
}

const DIMENSIONS = [
  { key: 'correctness', prompt: `You are a senior Python correctness reviewer. ${FEATURE}\n${FILES}\n\nHunt for REAL logic defects in the guard: thread-safety of the once-per-day check (concurrent symbol fetches), state file read/write atomicity on Windows, IST midnight date-boundary races with the scheduler's daily reauth, the semantics of adopting the new IP as baseline on detection (does it correctly skip only today and clear next day?), fail-open behavior, and whether shoonya_should_skip()'s lazy path could block the router fetch deadline. Return ONLY findings that are real and actionable, most severe first. Ignore style nits.` },
  { key: 'integration', prompt: `You are a senior integration reviewer. ${FEATURE}\n${FILES}\n\nVerify the wiring end-to-end: does the router short-circuit actually cause fallback for MCX symbols (priority ["shoonya","dhan_commodity",...])? Does it correctly NOT fire for other sources? Does ShoonyaFetcher.login() short-circuit cover scheduler pre-auth, daily reauth, quota re-auth, and the bulk-quotes path? Are there import cycles or lazy-import hazards? Does it conflict with pipeline.py's existing _refresh_ip_async/check_ip_changed generic alert? Would the guard ever block legitimate Shoonya use (false positive)? Return ONLY real, actionable findings.` },
  { key: 'robustness', prompt: `You are a resilience/edge-case reviewer. ${FEATURE}\n${FILES}\n\nAttack the guard: corrupted/half-written state file, missing data dir, IPv6-only ISP, provider returning a private IP, IP detection timing out mid-scan, process restart mid-day, system clock changes affecting _today(), multiple processes (scheduler + dashboard + --once) writing the same state file, the tmp+replace write pattern under Windows file locks. Distinguish real failure modes from theoretical ones. Return ONLY real, actionable findings with concrete scenarios.` },
]

const results = await pipeline(
  DIMENSIONS,
  d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  review => parallel((review.findings || []).map(f => () =>
    agent(
      `You are a skeptical verifier. A reviewer claimed this defect in the Shoonya IP-guard feature:\n\nFile: ${f.file}\nClaim: ${f.summary}\nDetail: ${f.detail}\n\n${FEATURE}\n${FILES}\n\nRead the actual code. TRY TO REFUTE the claim. It is real only if you can trace a concrete inputs/state sequence in the actual code that produces the described wrong behavior, and it matters in practice. Default to isReal=false unless clearly confirmed.`,
      { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA }
    ).then(v => ({ ...f, verdict: v }))
  ))
)

const all = results.filter(Boolean).flat()
const confirmed = all.filter(f => f.verdict && f.verdict.isReal)
log(`${confirmed.length} confirmed of ${all.length} findings`)
return { confirmed: confirmed.map(f => ({ severity: f.severity, file: f.file, summary: f.summary, detail: f.detail, suggestion: f.suggestion })) }
