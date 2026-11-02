import { ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, PALETTE_AREA, THEMES_AREA, host } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// Theme.
const theme = {
  name: 'hermes-control',
  label: 'Hermes Control',
  description: 'Warm paper surfaces and quiet terracotta accents.',
  typography: {
    fontSans: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontMono: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  },
  colors: {
    background: '#f5f4ed', foreground: '#141413', card: '#f5f4ed', cardForeground: '#141413',
    muted: '#efede4', mutedForeground: '#5e5d59', popover: '#faf9f5', popoverForeground: '#141413',
    primary: '#c96442', primaryForeground: '#faf9f5', secondary: '#e8e6dc', secondaryForeground: '#3d3d3a',
    accent: '#f0eee6', accentForeground: '#141413', border: '#e8e6dc', input: '#e8e6dc',
    ring: '#c96442', midground: '#c96442', destructive: '#b53333', destructiveForeground: '#faf9f5',
    sidebarBackground: '#efede4', sidebarBorder: '#e8e6dc', userBubble: '#efe9df', userBubbleBorder: '#e8e6dc',
  },
  darkColors: {
    background: '#1a1918', foreground: '#ece9df', card: '#262624', cardForeground: '#ece9df',
    muted: '#262624', mutedForeground: '#b0aea5', popover: '#30302e', popoverForeground: '#ece9df',
    primary: '#d97757', primaryForeground: '#141413', secondary: '#30302e', secondaryForeground: '#e8e6dc',
    accent: '#30302e', accentForeground: '#ece9df', border: '#30302e', input: '#30302e',
    ring: '#d97757', midground: '#d97757', destructive: '#e2715f', destructiveForeground: '#141413',
    sidebarBackground: '#141413', sidebarBorder: '#262624', userBubble: '#30302e', userBubbleBorder: '#3d3d3a',
  },
}
// End theme.

// Pure helpers.
function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return 'now'
  const n = Math.floor(seconds)
  if (n < 60) return `${n}s`
  if (n < 3600) return `${Math.floor(n / 60)}m ${n % 60}s`
  if (n < 86400) return `${Math.floor(n / 3600)}h ${Math.floor(n % 3600 / 60)}m`
  return `${Math.floor(n / 86400)}d ${Math.floor(n % 86400 / 3600)}h`
}

function resetText(epoch, now) {
  return epoch == null || epoch <= now ? 'now' : formatDuration(epoch - now)
}

function chipText(status, nowSeconds, offline = false) {
  if (offline || status?.error) return 'Hermes Control · offline'
  if (!status) return 'Hermes Control'
  const chip = status.chip || {}
  const engine = chip.engine || 'Task'
  switch (chip.kind) {
    case 'running':
      return `${engine} running` + (chip.started_at == null ? '' :
        ` · ${formatDuration(Math.max(0, nowSeconds - chip.started_at))}`)
    case 'blocked':
      return `${engine} blocked` + (chip.resets_at == null ? '' : ` · resets ${resetText(chip.resets_at, nowSeconds)}`)
    case 'unproven': {
      const count = status.proof?.not_proven?.length || 0
      return count ? `${count} not proven` + (chip.task_id ? ` · ${chip.task_id}` : '') : 'Not proven'
    }
    case 'near':
      return `${engine} ${Math.round(chip.percent)}% · near cap`
    default:
      return chip.engine != null && chip.percent != null ? `Idle · ${chip.engine} ${Math.round(chip.percent)}%` : 'Idle'
  }
}

function proofText(proof) {
  if (!proof) return ''
  if (!proof.written) return 'Reviews only'
  return `${proof.proven}/${proof.written} proven` + (proof.red_green > 0 ? ` · ${proof.red_green} red-green` : '')
}

function notifyText(status) {
  const run = status.last_run
  const proof = status.proof
  let text = `Run ${run.state} · ` + (proof && proof.written > 0 ? proofText(proof) : `${(run.task_ids || []).length} tasks`)
  if (proof?.not_proven?.length) text += ` · not proven: ${proof.not_proven.join(', ')}`
  return text
}

function fullestWindows(windows) {
  return [...(windows || [])].sort((a, b) => (b.used_percent ?? -1) - (a.used_percent ?? -1)).slice(0, 2)
}

function clampPercent(value) {
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0
}

function workState(usage) {
  if (usage.paused) return 'Paused'
  if (usage.blocked) return 'Blocked'
  if (usage.held_back != null) return 'Holding work back'
  return 'Taking work'
}

function barTone(usage, percent) {
  if (usage.report_only) return 'hc-report'
  if (percent == null || usage.cap == null) return 'hc-normal'
  if (percent >= usage.cap) return 'hc-over'
  if (percent >= usage.cap * 0.75) return 'hc-near'
  return 'hc-normal'
}

function usageWord(usage, percent) {
  const tone = barTone(usage, percent)
  return tone === 'hc-over' ? 'over cap' : tone === 'hc-near' ? 'near cap' : ''
}

function errorText(error) {
  return error?.message || String(error || 'Request failed')
}

// Explicitly select only the public fields used by the views.
function usageRows(answer) {
  return Object.entries(answer?.engines || {}).map(([name, row]) => ({
    name, windows: (row.windows || []).map(w => ({ label: w.label, used_percent: w.used_percent, resets_at: w.resets_at })),
    blocked: row.blocked, cap: row.cap, report_only: row.report_only, paused: row.paused,
    pause_problem: row.pause_problem, held_back: row.held_back, source: row.source, plan: row.plan, error: row.error,
  }))
}
function taskResult(task) {
  const status = task?.status
  const summary = task?.summary || {}
  const verdict = summary.verdict
  const neutral = 'hc-run-neutral'
  const red = 'hc-run-failed'
  const yellow = 'hc-run-other'
  const reason = summary.unproven_reason || task?.status_reason
  let word, dot, explanation
  if (status === 'running') return { word: status, dot: 'hc-run-active', lines: [] }
  if (['waiting', 'paused', 'stopped'].includes(status))
    return { word: status, dot: yellow, lines: task.status_reason ? [task.status_reason] : [] }
  if (status === 'failed') {
    word = 'failed'; dot = red; explanation = task.status_reason || summary.unproven_reason
  } else if (task?.policy === 'read-only') {
    return { word: 'review only', dot: neutral, lines: [] }
  } else {
    const results = {
      'not-committed': ['no commit', red, reason],
      'red-green-proven': ['proven (red-green)', 'hc-run-passed'],
      'tests-passed': ['tests passed', 'hc-run-passed'],
      'red-passed': ['tests passed, not red-green', yellow, summary.proof?.reason],
      'red-skipped': ['tests passed, not red-green', yellow, summary.proof?.reason],
      'red-inconclusive': ['tests passed, not red-green', yellow, summary.proof?.reason],
      'no-verify': ['no check', yellow],
      unproven: ['not proven', red, summary.unproven_reason],
      'verify-failed': ['check failed', red, reason],
      'verify-blocked': ['check blocked', red, reason],
    }
    const result = Object.hasOwn(results, verdict) ? results[verdict] :
      (status === 'unproven' && !verdict ? ['not proven', red, reason] : [verdict || status || 'unknown', neutral])
    word = result[0]; dot = result[1]; explanation = result[2]
  }
  const lines = explanation ? [explanation] : []
  let output = ''
  if (['verify-failed', 'verify-blocked'].includes(verdict)) {
    const verify = summary.verify || task?.verify
    if (verdict === 'verify-failed' && verify?.exit_code != null &&
        !explanation?.includes(`(exit ${verify.exit_code})`)) lines.push(`exit ${verify.exit_code}`)
    output = tailLines(verify?.output_tail)
  }
  return { word, dot, lines, output }
}

function verdictText(task) {
  return taskResult(task).word
}

function taskDot(task) {
  return taskResult(task).dot
}

function taskExtraLines(task) {
  const result = taskResult(task)
  return result.output ? [...result.lines, result.output] : result.lines
}

function repoName(path) {
  const trimmed = (path || '').replace(/\/+$/, '')
  const name = trimmed.split('/').pop() || ''
  const worktree = trimmed.lastIndexOf('/.worktrees/')
  return worktree < 0 ? name : `${trimmed.slice(0, worktree).split('/').pop()} · ${name}`
}

function runAge(epoch, now = Date.now() / 1000) {
  if (epoch == null || !Number.isFinite(epoch)) return 'unknown'
  return `${formatDuration(Math.max(0, now - epoch)).split(' ')[0]} ago`
}

function tailLines(output) {
  return (output || '').replace(/\r?\n$/, '').split(/\r?\n/).slice(-8).join('\n')
}

function runDot(state) {
  if (state === 'running') return 'hc-run-active'
  if (['failed', 'verify-failed'].includes(state)) return 'hc-run-failed'
  if (['finished', 'done'].includes(state)) return 'hc-run-passed'
  return 'hc-run-other'
}

function runOutcomeDot(state, tasks) {
  if (!['finished', 'done'].includes(state) || !Array.isArray(tasks) || !tasks.length) return runDot(state)
  const dots = tasks.map(task => taskResult(task).dot)
  if (dots.includes('hc-run-failed')) return 'hc-run-failed'
  if (dots.includes('hc-run-other')) return 'hc-run-other'
  return 'hc-run-passed'
}
function stepText(step) {
  const clean = value => {
    let text = String(value ?? '')
    const wrapper = text.match(/^\/bin\/(?:zsh|bash|sh) -lc /)
    if (wrapper) {
      text = text.slice(wrapper[0].length)
      const quote = text[0]
      if (quote === "'" || quote === '"') {
        text = text.slice(1)
        if (text.endsWith(quote)) text = text.slice(0, -1)
      }
    }
    return text.replace(/\/(?:Users|home)\/[^\/\s'"]+/g, '~').replace(/\s+/g, ' ').trim()
  }
  let text = ''
  if (step?.type === 'tool') text = step.name + (step.detail ? ` ${clean(step.detail)}` : '')
  else if (step?.type === 'text' || step?.type === 'error') text = clean(step.text)
  return text.length > 120 ? text.slice(0, 119) + '…' : text
}

function livePhase(task) {
  if (task.policy === 'read-only') return 'review'
  return task.attempts?.at(-1)?.result == null ? 'write' : 'check'
}

function liveTasks(detail, now) {
  if (detail?.state !== 'running') return []
  return (detail.tasks || []).filter(task => task.status === 'running').map(task => {
    const attempt = task.attempts?.at(-1)
    return {
      id: task.id, engine: attempt?.engine, model: attempt?.model,
      elapsed: attempt?.started_at == null ? '' : formatDuration(Math.max(0, now - attempt.started_at)),
      phase: livePhase(task),
      steps: (attempt?.steps || []).map(step => ({
        text: stepText(step), error: step.type === 'error' || (step.type === 'tool' && !!step.is_error),
      })).filter(step => step.text).slice(-3),
    }
  })
}
// End pure helpers.

const styles = `
.hc-chip { display:inline-flex; align-items:center; gap:5px; min-width:0; max-width:100%; height:100%; color:inherit; font:inherit; white-space:nowrap; overflow:hidden; }
.hc-chip-text { overflow:hidden; text-overflow:ellipsis; }
.hc-mark { width:11px; height:11px; flex:none; }
.hc-dot { display:inline-block; width:6px; height:6px; border-radius:50%; flex:none; }
.hc-running { background:var(--ui-accent); }
.hc-blocked { background:var(--ui-red); }
.hc-unproven, .hc-near-dot { background:var(--ui-yellow); }
.hc-popover.hc-popover { width:360px; max-width:calc(100vw - 24px); padding:0; }
.hc-popover-body { padding:14px 14px 10px; color:var(--ui-text-primary); font-size:13px; line-height:1.5; }
.hc-popover-body h2 { margin:0 0 8px; font-size:14px; font-weight:500; }
.hc-pop-row { display:grid; grid-template-columns:70px repeat(2,minmax(0,1fr)); gap:10px; padding:9px 0; border-top:1px solid var(--ui-stroke-secondary); }
.hc-pop-window { display:grid; gap:5px; min-width:0; }
.hc-pop-links, .hc-actions { display:flex; flex-wrap:wrap; gap:8px; }
.hc-pop-links, .hc-last-run { padding-top:10px; margin-top:8px; border-top:1px solid var(--ui-stroke-secondary); }
.hc-page { box-sizing:border-box; padding:28px 36px 40px; max-width:1120px; width:100%; height:100%; overflow:auto; color:var(--ui-text-primary); font-size:14px; line-height:1.5; }
.hc-page-header { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:18px; }
.hc-title { font-family:inherit; font-weight:500; font-size:26px; line-height:1.2; margin:0; }
.hc-engine-name { font-family:inherit; font-weight:500; font-size:20px; margin:0 0 4px; }
:root[data-hermes-theme="hermes-control"] .hc-title, :root[data-hermes-theme="hermes-control"] .hc-engine-name { font-family:ui-serif,"New York",Georgia,serif; }
.hc-button { border:1px solid var(--ui-stroke-primary); border-radius:8px; padding:5px 12px; background:var(--ui-sidebar-surface-background); color:var(--ui-text-secondary); font:inherit; font-size:13px; cursor:pointer; }
.hc-button:hover { background:var(--ui-row-hover-background); color:var(--ui-text-primary); }
.hc-button:focus-visible { outline:2px solid var(--ui-accent); outline-offset:2px; }
.hc-button:disabled { cursor:wait; }
.hc-muted, .hc-detail, .hc-problem { color:var(--ui-text-secondary); font-size:13px; overflow-wrap:anywhere; }
.hc-strip { display:flex; flex-wrap:wrap; gap:12px 28px; padding:12px 0; border-top:1px solid var(--ui-stroke-secondary); border-bottom:1px solid var(--ui-stroke-secondary); font-size:13px; }
.hc-engine-row { display:grid; grid-template-columns:220px minmax(0,1fr); gap:32px; padding:22px 0; border-bottom:1px solid var(--ui-stroke-secondary); }
.hc-engine-info, .hc-windows { display:grid; align-content:start; gap:8px; min-width:0; }
.hc-work { display:flex; align-items:center; gap:7px; margin-top:6px; }
.hc-taking { background:var(--ui-green); }
.hc-paused { background:var(--ui-yellow); }
.hc-window { display:grid; grid-template-columns:minmax(100px,1fr) minmax(70px,1fr) 48px 140px; align-items:center; gap:12px; font-size:13px; }
.hc-window-label { overflow-wrap:anywhere; color:var(--ui-text-secondary); }
.hc-number { font-variant-numeric:tabular-nums; }
.hc-meter { position:relative; height:6px; border-radius:3px; background:var(--ui-stroke-secondary); }
.hc-fill { position:absolute; top:0; bottom:0; left:0; border-radius:3px; background:var(--ui-text-secondary); }
.hc-near .hc-fill { background:var(--ui-yellow); }
.hc-over .hc-fill { background:var(--ui-red); }
.hc-report .hc-fill { background:repeating-linear-gradient(135deg,var(--ui-text-secondary) 0 3px,transparent 3px 5px); }
.hc-cap { position:absolute; top:-3px; bottom:-3px; width:1.5px; background:var(--ui-text-primary); }
.hc-skeleton { padding:8px 10px; border-radius:6px; background:var(--ui-row-hover-background); color:var(--ui-text-secondary); font-size:13px; }
.hc-last-run { overflow-wrap:anywhere; }
@media (max-width:760px) { .hc-page { padding:20px; } .hc-engine-row { grid-template-columns:1fr; gap:16px; } .hc-window { grid-template-columns:1fr 1fr; } .hc-page-header { flex-wrap:wrap; } }
.hc-live { display:block; padding:12px 16px; border-radius:8px; background:var(--ui-row-hover-background); margin-bottom:14px; }
.hc-live-current { font-weight:600; color:var(--ui-accent); }
.hc-live-step { font-size:13px; color:var(--ui-text-secondary); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hc-live-error { color:var(--ui-red); }
.hc-run { padding:18px 0; border-bottom:1px solid var(--ui-stroke-secondary); }
.hc-run-heading, .hc-run-task-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; overflow-wrap:anywhere; }
.hc-run-state { display:inline-flex; align-items:center; gap:7px; }
.hc-run-id { font-weight:600; margin-right:5px; }
.hc-run-secondary { color:var(--ui-text-secondary); font-size:13px; }
.hc-run-repo { margin:4px 0 12px; overflow-wrap:anywhere; }
.hc-run-task { margin:10px 0 0 16px; font-size:14px; }
.hc-run-failure { margin:8px 0 12px 13px; color:var(--ui-text-secondary); font-size:13px; overflow-wrap:anywhere; }
.hc-run-output { margin:6px 0 0; padding:10px 12px; border-radius:6px; background:var(--ui-row-hover-background); color:var(--ui-text-primary); font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
.hc-run-active { background:var(--ui-accent); }
.hc-run-failed { background:var(--ui-red); }
.hc-run-passed { background:var(--ui-green); }
.hc-run-other { background:var(--ui-yellow); }
.hc-run-neutral { background:var(--ui-stroke-primary); }
`

// A registration owns its store: reloads cannot reuse data or notification history.
function createStatusStore(ctx) {
  let status = null
  let error = false
  let disposed = false
  let pending = false
  const subscribers = new Set()
  const notified = new Set()
  const emit = () => subscribers.forEach(fn => fn())
  const poll = async () => {
    if (pending || disposed) return
    pending = true
    try {
      const next = await ctx.rest('/status', { timeoutMs: 10000 })
      if (disposed) return
      const previous = status?.last_run
      status = next
      error = false
      const run = next.last_run
      if (previous?.state === 'running' && run && previous.run_id === run.run_id &&
          run.state !== 'running' && !notified.has(run.run_id)) {
        notified.add(run.run_id)
        try {
          await ctx.os?.notify?.({ title: 'Hermes Control', body: notifyText(next), activate: '/hermes-control/runs' })
        } catch { /* OS delivery does not affect backend health. */ }
      }
    } catch {
      if (!disposed) error = true
    } finally {
      pending = false
      if (!disposed) emit()
    }
  }
  ctx.onDispose(() => { disposed = true; subscribers.clear(); notified.clear() })
  ctx.setInterval(poll, 15000)
  ctx.setInterval(() => { if (!error && status?.chip?.kind === 'running') emit() }, 1000)
  void poll()
  return {
    read: () => ({ status, error }),
    subscribe: fn => { subscribers.add(fn); return () => subscribers.delete(fn) },
  }
}

function useStatus(store) {
  const [, setRevision] = useState(0)
  useEffect(() => {
    const update = () => setRevision(n => n + 1)
    const unsubscribe = store.subscribe(update)
    update()
    return unsubscribe
  }, [store])
  return store.read()
}

function StatusChip({ store }) {
  const { status, error } = useStatus(store)
  const kind = error ? null : status?.chip?.kind
  const tone = { running: 'hc-running', blocked: 'hc-blocked', unproven: 'hc-unproven', near: 'hc-near-dot' }[kind]
  return jsxs('span', { className: 'hc-chip', children: [
    jsx('svg', { className: 'hc-mark', viewBox: '0 0 12 12', 'aria-hidden': true, children:
      jsx('path', { d: 'M2 1 L2 11 M10 1 L10 11 M2 6 L10 6', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5 }) }),
    tone ? jsx('span', { className: `hc-dot ${tone}`, 'aria-hidden': true }) : null,
    jsx('span', { className: 'hc-chip-text', children: chipText(status, Date.now() / 1000, error) }),
  ] })
}

function Meter({ usage, window: reading }) {
  return jsxs('div', { className: `hc-meter ${barTone(usage, reading.used_percent)}`, 'aria-hidden': true, children: [
    reading.used_percent != null ? jsx('span', { className: 'hc-fill', style: { width: `${clampPercent(reading.used_percent)}%` } }) : null,
    !usage.report_only && usage.cap != null ? jsx('span', { className: 'hc-cap', style: { left: `${clampPercent(usage.cap)}%` } }) : null,
  ] })
}

function UsageWindow({ usage, reading, compact = false }) {
  const word = usageWord(usage, reading.used_percent)
  return jsxs('div', { className: compact ? 'hc-pop-window' : 'hc-window', children: [
    jsx('span', { className: 'hc-window-label', children: reading.label || 'Usage' }),
    jsx(Meter, { usage, window: reading }),
    jsx('span', { className: 'hc-number', children: reading.used_percent == null ? 'no reading' : `${Math.round(reading.used_percent)}%${word ? ` · ${word}` : ''}` }),
    !compact ? jsx('span', { className: 'hc-muted', children: reading.resets_at == null ? 'Reset time unknown' : `resets in ${resetText(reading.resets_at, Date.now() / 1000)}` }) : null,
  ] })
}

function StatusPopover({ ctx, store, close }) {
  const { status, error: offline } = useStatus(store)
  const [limits, setLimits] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const answer = await ctx.rest('/limits', { timeoutMs: 15000 })
        if (active) setLimits({ rows: usageRows(answer), problem: answer.problem })
      } catch (err) { if (active) setError(errorText(err)) }
    }
    void load()
    return () => { active = false }
  }, [ctx])
  const rows = limits?.rows || (status?.engines || []).map(row => ({
    name: row.name, cap: row.cap, report_only: row.report_only,
    windows: [{ label: row.window || 'Usage', used_percent: row.percent, resets_at: row.resets_at }],
  }))
  const run = status?.last_run
  const open = path => { close(); host.navigate(path) }
  return jsxs('div', { className: 'hc-popover-body', children: [
    jsx('h2', { children: 'Hermes Control' }),
    !limits && !error ? jsx('p', { className: 'hc-muted', children: 'Loading usage…' }) : null,
    offline ? jsx('p', { className: 'hc-problem', children: 'Hermes Control · offline' }) : null,
    error || limits?.problem ? jsx('p', { className: 'hc-problem', role: 'status', children: error || limits.problem }) : null,
    ...rows.map(row => jsxs('div', { className: 'hc-pop-row', children: [
      jsxs('div', { children: [row.name, row.report_only ? jsx('div', { className: 'hc-muted', children: 'report only' }) : null,
        row.error ? jsx('div', { className: 'hc-problem', children: row.error }) : null] }),
      ...fullestWindows(row.windows).map((reading, i) => jsx(UsageWindow, { usage: row, reading, compact: true }, i)),
    ] }, row.name)),
    run ? jsxs('div', { className: 'hc-last-run', children: [
      jsx('div', { children: `${run.run_id} · ${run.state}` }),
      jsx('div', { className: 'hc-muted', children: proofText(status.proof) }),
      status.proof?.not_proven?.length ? jsx('div', { className: 'hc-detail', children: `Not proven: ${status.proof.not_proven.join(', ')}` }) : null,
    ] }) : null,
    jsxs('div', { className: 'hc-pop-links', children: [
      jsx('button', { className: 'hc-button', type: 'button', onClick: () => open('/hermes-control/engines'), children: 'Open Engines' }),
      jsx('button', { className: 'hc-button', type: 'button', onClick: () => open('/hermes-control/runs'), children: 'Open Runs' }),
    ] }),
  ] })
}

function EngineRow({ name, engine, usage, loading }) {
  const state = usage ? workState(usage) : null
  const tone = state === 'Taking work' ? 'hc-taking' : state === 'Paused' ? 'hc-paused' : 'hc-blocked'
  return jsxs('section', { className: 'hc-engine-row', 'aria-label': name, children: [
    jsxs('div', { className: 'hc-engine-info', children: [
      jsx('h2', { className: 'hc-engine-name', children: name }),
      loading ? jsx('div', { className: 'hc-skeleton', role: 'status', children: 'Checking installation and sign-in…' }) :
        engine ? jsxs('div', { className: 'hc-detail', children: [
          engine.version ? jsx('div', { children: engine.version }) : null,
          jsx('div', { children: !engine.installed ? 'Not installed' : engine.signed_in ? 'Signed in' : 'Not signed in' }),
          engine.problem ? jsx('div', { children: engine.problem }) : null,
          engine.reasoning_summary != null ? jsx('div', { children: `Reasoning summary: ${engine.reasoning_summary}` }) : null,
        ] }) : jsx('div', { className: 'hc-detail', children: 'Installation status unavailable' }),
      usage ? jsxs('div', { className: 'hc-detail', children: [
        usage.plan ? jsx('div', { children: `Plan: ${usage.plan}` }) : null,
        jsxs('div', { className: 'hc-work', children: [jsx('span', { className: `hc-dot ${tone}`, 'aria-hidden': true }), state] }),
        !usage.report_only ? jsx('div', { children: usage.cap == null ? 'Cap unavailable' : `cap ${usage.cap}%` }) : null,
        usage.source ? jsx('div', { children: `Source: ${usage.source}` }) : null,
        usage.held_back != null ? jsx('div', { children: usage.held_back }) : null,
        usage.pause_problem ? jsx('div', { children: usage.pause_problem }) : null,
      ] }) : null,
    ] }),
    jsxs('div', { className: 'hc-windows', children: [
      !usage ? jsx('div', { className: 'hc-skeleton', children: 'Usage unavailable' }) : null,
      ...(usage?.windows || []).map((reading, i) => jsx(UsageWindow, { usage, reading }, i)),
      usage?.error ? jsx('div', { className: 'hc-problem', children: usage.error }) : null,
      usage?.report_only ? jsx('div', { className: 'hc-detail', children: 'Report only: this reading never holds work back.' }) : null,
    ] }),
  ] })
}

const DASHBOARD_URL = 'http://127.0.0.1:9119/engines'

function EnginesPage({ ctx, store }) {
  const { status } = useStatus(store)
  const [engines, setEngines] = useState(null)
  const [planner, setPlanner] = useState(null)
  const [limits, setLimits] = useState(null)
  const [errors, setErrors] = useState({})
  const [loading, setLoading] = useState({ engines: true, planner: true, limits: true })
  const generation = useRef(0)
  const load = refresh => {
    const current = ++generation.current
    setErrors({})
    setLoading({ engines: true, planner: true, limits: true })
    const request = async (key, path, save) => {
      try {
        const answer = await ctx.rest(path, { timeoutMs: 30000 })
        if (generation.current === current) save(answer)
      } catch (err) {
        if (generation.current === current) setErrors(old => ({ ...old, [key]: errorText(err) }))
      } finally {
        if (generation.current === current) setLoading(old => ({ ...old, [key]: false }))
      }
    }
    void request('engines', '/engines', setEngines)
    void request('planner', '/planner', setPlanner)
    void request('limits', refresh ? '/limits?refresh=true' : '/limits', answer => setLimits({ rows: usageRows(answer), problem: answer.problem }))
  }
  useEffect(() => {
    load(false)
    return () => { generation.current++ }
  }, [ctx])
  const rows = limits?.rows || []
  const names = [...new Set([...(status?.engines || []).map(row => row.name),
    ...rows.map(row => row.name), ...(engines?.engines || []).map(row => row.name)])]
  const busy = Object.values(loading).some(Boolean)
  const openDashboard = async () => {
    try {
      const opened = await ctx.os?.openExternal?.(DASHBOARD_URL)
      if (!opened) throw new Error('Could not open the dashboard')
    } catch (err) { setErrors(old => ({ ...old, dashboard: errorText(err) })) }
  }
  return jsxs('main', { className: 'hc-page', children: [
    jsxs('div', { className: 'hc-page-header', children: [
      jsx('h1', { className: 'hc-title', children: 'Engines' }),
      jsxs('div', { className: 'hc-actions', children: [
        jsx('button', { className: 'hc-button', type: 'button', disabled: busy, onClick: () => load(true), children: 'Refresh' }),
        jsx('button', { className: 'hc-button', type: 'button', onClick: openDashboard, children: 'Edit in dashboard' }),
      ] }),
    ] }),
    jsxs('div', { className: 'hc-strip', children: [
      jsx('div', { children: planner ? `Planner: ${planner.provider || 'unavailable'} · ${planner.model || 'unavailable'}` : loading.planner ? 'Planner: loading…' : 'Planner: unavailable' }),
      jsx('div', { children: !engines ? loading.engines ? 'Verify sandbox: checking…' : 'Verify sandbox: unknown' : engines.sandbox?.available ? 'Verify sandbox: available' : 'Verify sandbox: unavailable (needs the codex CLI installed)' }),
      limits?.problem ? jsx('div', { className: 'hc-problem', children: limits.problem }) : null,
    ] }),
    ...Object.entries(errors).map(([key, message]) => jsx('p', { className: 'hc-problem', role: 'status', children: `${key}: ${message}` }, key)),
    !names.length ? jsx('p', { className: 'hc-skeleton', children: busy ? 'Loading engines…' : 'No engine readings available.' }) : null,
    ...names.map(name => jsx(EngineRow, { name, loading: loading.engines,
      engine: engines?.engines?.find(row => row.name === name), usage: rows.find(row => row.name === name) }, name)),
  ] })
}

function RunsPage({ ctx }) {
  const [runs, setRuns] = useState(null)
  const [details, setDetails] = useState({})
  const [error, setError] = useState('')
  const [detailErrors, setDetailErrors] = useState({})
  const [dashboardError, setDashboardError] = useState('')
  const mounted = useRef(false)
  const generation = useRef(0)
  const cache = useRef(new Map())
  const lastRuns = useRef([])
  const activeLoads = useRef(0)
  const load = async () => {
    activeLoads.current++
    const current = ++generation.current
    try {
      const answer = await ctx.rest('/runs?limit=30', { timeoutMs: 10000 })
      if (generation.current !== current) return
      const rows = answer?.runs || []
      lastRuns.current = rows
      setRuns(rows)
      setError('')
      await Promise.all(rows.map(async run => {
        let entry = cache.current.get(run.run_id)
        try {
          if (!entry || (!entry.pending && (run.state === 'running' ||
              entry.state !== run.state || entry.updated_at !== run.updated_at))) {
            entry = { state: run.state, updated_at: run.updated_at, pending: true }
            cache.current.set(run.run_id, entry)
            entry.promise = ctx.rest(`/runs/${encodeURIComponent(run.run_id)}`, { timeoutMs: 10000 })
              .finally(() => { entry.pending = false })
          }
          const detail = await entry.promise
          if (generation.current !== current) return
          setDetails(old => old[run.run_id] === detail ? old : { ...old, [run.run_id]: detail })
          setDetailErrors(old => {
            if (!old[run.run_id]) return old
            const next = { ...old }
            delete next[run.run_id]
            return next
          })
        } catch (err) {
          if (cache.current.get(run.run_id) === entry) cache.current.delete(run.run_id)
          if (generation.current !== current) return
          setDetailErrors(old => ({ ...old, [run.run_id]: errorText(err) }))
        }
      }))
    } catch (err) {
      if (generation.current === current) setError(errorText(err))
    } finally {
      activeLoads.current--
    }
  }
  useEffect(() => {
    mounted.current = true
    void load()
    const dispose = ctx.setInterval(() => { void load() }, 10000)
    const disposeLive = ctx.setInterval(() => {
      if (!activeLoads.current && lastRuns.current.some(run => run.state === 'running')) void load()
    }, 3000)
    return () => { mounted.current = false; generation.current++; dispose(); disposeLive() }
  }, [ctx])
  const openDashboard = async () => {
    try {
      const opened = await ctx.os?.openExternal?.(DASHBOARD_URL)
      if (!opened) throw new Error('Could not open the dashboard')
      if (mounted.current) setDashboardError('')
    } catch (err) { if (mounted.current) setDashboardError(errorText(err)) }
  }
  return jsxs('main', { className: 'hc-page', children: [
    jsxs('div', { className: 'hc-page-header', children: [
      jsx('h1', { className: 'hc-title', children: 'Runs' }),
      jsxs('div', { className: 'hc-actions', children: [
        jsx('button', { className: 'hc-button', type: 'button', onClick: () => { void load() }, children: 'Refresh' }),
        jsx('button', { className: 'hc-button', type: 'button', onClick: openDashboard, children: 'Open in dashboard' }),
      ] }),
    ] }),
    error ? jsx('p', { className: 'hc-problem', role: 'status', children: error }) : null,
    ...(dashboardError ? [jsx('p', { className: 'hc-run-secondary', role: 'status', children: dashboardError })] : []),
    ...(runs || []).filter(run => run.state === 'running' && !detailErrors[run.run_id]).flatMap(run =>
      liveTasks(details[run.run_id], Date.now() / 1000).map(task => jsxs('section', {
        className: 'hc-live', children: [
          jsxs('div', { className: 'hc-run-heading', children: [
            jsxs('span', { className: 'hc-run-state', children: [
              jsx('span', { className: 'hc-dot hc-run-active', 'aria-hidden': true }),
              jsx('span', { className: 'hc-run-id', children: task.id }),
            ] }),
            jsx('span', { className: 'hc-run-secondary', children: [task.engine, task.model].filter(Boolean).join(' · ') }),
            jsx('span', { className: 'hc-run-secondary', children: task.elapsed }),
          ] }),
          jsxs('div', { children: (task.phase === 'review' ? ['review', 'done'] : ['write', 'check', 'done'])
            .flatMap((phase, index) => [
              ...(index ? [' → '] : []),
              jsx('span', { className: phase === task.phase ? 'hc-live-current' : 'hc-run-secondary', children: phase }, phase),
            ]) }),
          ...task.steps.map((step, index) => jsx('div', {
            className: `hc-live-step${step.error ? ' hc-live-error' : ''}`, children: step.text,
          }, index)),
        ],
      }, `${run.run_id}:${task.id}`))),
    runs === null && !error ? jsx('p', { className: 'hc-skeleton', children: 'Loading runs…' }) : null,
    runs?.length === 0 ? jsx('p', { className: 'hc-run-secondary', children: 'No recent runs.' }) : null,
    ...(runs || []).map(run => {
      const detail = details[run.run_id]
      const state = detail?.state || run.state || 'unknown'
      return jsxs('section', { className: 'hc-run', children: [
        jsxs('div', { className: 'hc-run-heading', children: [
          jsxs('span', { className: 'hc-run-state', children: [
            jsx('span', { className: `hc-dot ${runOutcomeDot(state, detail?.tasks)}`, 'aria-hidden': true }), state,
          ] }),
          jsx('span', { children: (run.run_id || '').slice(0, 12) }),
          jsx('span', { className: 'hc-run-secondary', children: runAge(run.updated_at) }),
        ] }),
        detailErrors[run.run_id] ? jsx('p', { className: 'hc-run-secondary', role: 'status', children: detailErrors[run.run_id] }) : null,
        jsx('div', { className: 'hc-run-secondary hc-run-repo', children: repoName(run.repo) }),
        ...(detail?.tasks || run.tasks || []).filter(Boolean).map(task => {
          const attempt = task.attempts?.[task.attempts.length - 1]
          const engine = attempt?.engine || run.tasks?.find(t => t?.id === task.id)?.engine || task.engine
          const model = attempt?.model || task.model
          const result = detail ? taskResult(task) : { word: task.status || 'unknown', dot: 'hc-run-neutral', lines: [] }
          return jsxs('div', { className: 'hc-run-task', children: [
            jsxs('div', { className: 'hc-run-task-row', children: [
              jsxs('span', { className: 'hc-run-state', children: [
                jsx('span', { className: `hc-dot ${result.dot}`, 'aria-hidden': true }),
                jsx('span', { className: 'hc-run-id', children: task.id }),
                jsx('span', { children: result.word }),
              ] }),
              jsx('span', { className: 'hc-run-secondary', children: [engine, model].filter(Boolean).join(' · ') }),
              task.policy === 'write' && task.summary?.diff_stat ? jsx('span', { children: task.summary.diff_stat }) : null,
              task.status !== 'running' && attempt?.started_at != null && attempt?.ended_at != null ? jsx('span', {
                className: 'hc-run-secondary', children: formatDuration(attempt.ended_at - attempt.started_at),
              }) : null,
            ] }),
            result.lines.length || result.output ? jsxs('div', { className: 'hc-run-failure', children: [
              ...result.lines.map((line, index) => jsx('div', { children: line }, index)),
              result.output ? jsx('pre', { className: 'hc-run-output', children: result.output }) : null,
            ] }) : null,
          ] }, task.id)
        }),
      ] }, run.run_id)
    }),
  ] })
}

export default {
  id: 'hermes_control',
  name: 'Hermes Control',
  defaultEnabled: false,
  register(ctx) {
    const store = createStatusStore(ctx)
    const style = document.createElement('style')
    style.textContent = styles
    document.head.appendChild(style)
    ctx.onDispose(() => style.remove())
    if (typeof MutationObserver !== 'undefined' && document.body) {
      const selector = '[aria-label="HERMES AGENT"],[aria-label="HERMES CONTROL"]'
      const changed = new Set()
      function pruneDetached() {
        for (const element of changed) {
          if (!element.isConnected) changed.delete(element)
        }
      }
      function changeText(node, from, to) {
        if (node.nodeType === 3 && node.nodeValue.trim() === from) node.nodeValue = to
        for (const child of node.childNodes) changeText(child, from, to)
      }
      function applyWordmark(element) {
        changeText(element, 'HERMES AGENT', 'HERMES CONTROL')
        if (element.getAttribute('aria-label') !== 'HERMES CONTROL') {
          element.setAttribute('aria-label', 'HERMES CONTROL')
        }
        pruneDetached()
        if (element.isConnected) changed.add(element)
      }
      function inspect(node, descendants = false) {
        const element = node.nodeType === 1 ? node : node.parentElement
        const target = element?.closest(selector)
        if (target) applyWordmark(target)
        if (descendants && node.nodeType === 1) node.querySelectorAll(selector).forEach(applyWordmark)
      }
      let observer
      ctx.onDispose(() => {
        observer?.disconnect()
        pruneDetached()
        for (const element of changed) {
          changeText(element, 'HERMES CONTROL', 'HERMES AGENT')
          element.setAttribute('aria-label', 'HERMES AGENT')
        }
        changed.clear()
      })
      try {
        inspect(document.body, true)
        observer = new MutationObserver(records => {
          try {
            pruneDetached()
            for (const record of records) {
              inspect(record.target)
              for (const node of record.addedNodes || []) inspect(node, true)
            }
          } catch {
            observer.disconnect()
          }
        })
        observer.observe(document.body, {
          childList: true, subtree: true, characterData: true,
          attributes: true, attributeFilter: ['aria-label'],
        })
      } catch {
        observer?.disconnect()
      }
    }
    function ChipLabel() { return jsx(StatusChip, { store }) }
    function ChipPopover({ close }) { return jsx(StatusPopover, { ctx, store, close }) }
    ctx.registerMany([
      { id: 'theme', area: THEMES_AREA, data: theme },
      { id: 'chip', area: STATUSBAR_AREAS.right, data: {
        id: 'hermes-control-chip', variant: 'menu', toggleLabel: 'Hermes Control', label: jsx(ChipLabel, {}),
        menuContent: (close) => jsx(ChipPopover, { close }), menuAlign: 'end', menuClassName: 'hc-popover',
      } },
      { id: 'engines-page', area: ROUTES_AREA, data: { path: '/hermes-control/engines' }, render: () => jsx(EnginesPage, { ctx, store }) },
      { id: 'runs-page', area: ROUTES_AREA, data: { path: '/hermes-control/runs' }, render: () => jsx(RunsPage, { ctx }) },
      { id: 'engines-nav', area: SIDEBAR_NAV_AREA, data: { path: '/hermes-control/engines', label: 'Engines', codicon: 'dashboard' } },
      { id: 'runs-nav', area: SIDEBAR_NAV_AREA, data: { path: '/hermes-control/runs', label: 'Runs', codicon: 'list-unordered' } },
      { id: 'open-engines', area: PALETTE_AREA, data: { id: 'hermes-control.open-engines', label: 'Hermes Control: Open Engines', keywords: ['hermes', 'control', 'engines', 'usage'], run: () => host.navigate('/hermes-control/engines') } },
      { id: 'open-runs', area: PALETTE_AREA, data: { id: 'hermes-control.open-runs', label: 'Hermes Control: Open Runs', keywords: ['hermes', 'control', 'runs', 'proof'], run: () => host.navigate('/hermes-control/runs') } },
    ])
  },
}
