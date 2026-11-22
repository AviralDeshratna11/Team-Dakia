import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUp, Check, ChevronDown, ChevronRight, CircleAlert, CircleHelp, Copy, ExternalLink, Layers3, LoaderCircle, Mail, Plus, ShieldCheck, Sparkles, X } from 'lucide-react';
import { api, AccessDenied, setAccessCode } from './api/client';
import type { Run } from './types';
import './styles.css';
import { TrustCards, plain } from './components/TrustCards';

const money = (minor: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(minor / 100);
const terminals = new Set(['COMPLETED', 'COMPLETED_WITH_GAPS', 'FAILED', 'CANCELLED']);
const waiting = new Set(['WAITING_FOR_CLARIFICATION', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_HUMAN']);
// A turn ends where the agent hands the conversation back to you.
const handBack = new Set(['WAITING_FOR_CLARIFICATION', 'WAITING_FOR_HUMAN']);
const cancelWords = /^\s*(cancel|stop|abort)\b/i;
const examples = [
  'Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each.',
  'Source ESP32-S3 modules, a 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors. 500 of each, delivered to Bengaluru within three weeks.',
];
const leatherWords = /leather|tanner|handbag|wallet|belt|footwear|shoe|saddle|bags?\b/i;
const electronicsWords = /\b(?:esp32(?:-s3)?|ldo|capacitors?|resistors?|usb-?c|pcbs?|bom|mcus?|microcontrollers?|voltage regulators?|smd)\b/i;

// Terms the founder states in plain words ("ask me before any order over ₹40,000",
// "keep data spend under ₹1,000", "within 5 weeks") become the run's hard limits.
const AMOUNT = String.raw`(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|k|thousand)?\b`;
function rupees(num: string, unit?: string) {
  const n = Number(num.replace(/,/g, ''));
  const u = (unit ?? '').toLowerCase();
  return n * (/^(lakh|lac)/.test(u) ? 1e5 : /^(crore|cr)/.test(u) ? 1e7 : /^(k|thousand)$/.test(u) ? 1e3 : 1);
}
function readTerms(text: string) {
  const approval = new RegExp(String.raw`(?:ask|check with|confirm with|consult)\s+me\s+before\s+(?:any\s+|an?\s+)?(?:orders?|purchases?|spend(?:ing)?|payments?)\s+(?:over|above|exceeding|of more than|greater than|more than)\s+` + AMOUNT, 'i').exec(text)
    ?? new RegExp(String.raw`approv\w*\s+(?:required\s+|needed\s+)?(?:for\s+(?:any\s+)?orders?\s+)?(?:over|above)\s+` + AMOUNT, 'i').exec(text);
  const data = new RegExp(String.raw`(?:data|research)\s+(?:spend(?:ing)?|budget|costs?)\s+(?:under|below|within|less than|up to|max(?:imum)?(?:\s+of)?|of|to|at)?\s*` + AMOUNT, 'i').exec(text);
  const within = /\b(?:within|in|inside)\s+(\d+)\s+(day|week|month)s?\b/i.exec(text);
  const days = within ? Number(within[1]) * (/week/i.test(within[2]) ? 7 : /month/i.test(within[2]) ? 30 : 1) : 21;
  return {
    approval_threshold_minor: approval ? Math.round(rupees(approval[1], approval[2]) * 100) : 100000000,
    research_budget_minor: data ? Math.round(rupees(data[1], data[2]) * 100) : 200000,
    deadline: new Date(Date.now() + days * 86400000).toISOString(),
  };
}
const approveWords = /^\s*(approve|approved|yes|go ahead|confirm|ok(ay)?)\b/i;
const STEP: Record<string, string> = {
  CREATED: 'Received your request', PARSING: 'Understanding the request', DISCOVERING: 'Finding suppliers', VERIFYING: 'Checking suppliers',
  RESEARCHING: 'Filling information gaps', DECIDING: 'Comparing options', SCREENING: 'Compliance screen', EXECUTING: 'Preparing orders and RFQs',
  WAITING_FOR_CLARIFICATION: 'Need a detail from you', WAITING_FOR_APPROVAL: 'Waiting for your approval', COMPLETED: 'Done',
  COMPLETED_WITH_GAPS: 'Done, with gaps', FAILED: 'Stopped', WAITING_FOR_HUMAN: 'Need your help', CANCELLED: 'Cancelled',
};

type Health = { gemini_configured?: boolean; crustdata_configured?: boolean };
// Fetched once at startup, in parallel with the stylesheet; sending waits for it so the
// first message never falls back to sample data just because this reply was still in flight.
const healthPromise: Promise<Health> = fetch('/api/health').then(r => r.json() as Promise<Health>).catch(() => ({}));
type Entry = { kind: 'user'; text: string } | { kind: 'agent'; runId: string; segment: number } | { kind: 'note'; text: string; error?: boolean };
type Step = { id: string; title: string; detail: string; thought?: boolean };

const load = <T,>(key: string, fallback: T): T => { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) as T : fallback; } catch { return fallback; } };
const save = (key: string, value: unknown) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } };

/** Events belonging to one agent turn: a run is split at each clarification question. */
function segmentEvents(run: Run, segment: number) {
  const out: Run['events'] = [];
  let current = 0;
  for (const e of run.events) {
    if (current === segment) out.push(e);
    if (handBack.has(e.state)) current++;
    if (current > segment) break;
  }
  return out;
}
const segmentCount = (run: Run) => run.events.filter(e => handBack.has(e.state)).length;

function humanize(run: Run, e: Run['events'][number]) {
  const m = e.message;
  if (e.state === 'CREATED') return run.request.mode === 'live' ? 'Using live company data from Crustdata.' : 'Using sample supplier data for this demo.';
  if (m.startsWith('Gemini request parsing enabled')) return 'Reading your request with Gemini.';
  if (m.startsWith('Reading the request with the bounded')) return 'Reading your request.';
  if (m.startsWith('Clarification saved')) return 'Got your answer. Reading the request again.';
  return m.replace(/^Specifications saved; finding (fixture|live) suppliers\./, (_, mode) => mode === 'live' ? 'Requirements are clear. Searching live company data.' : 'Requirements are clear. Searching sample suppliers.');
}

function stepsFor(run: Run, events: Run['events']): Step[] {
  const steps: Step[] = [];
  const name = (id: string) => run.suppliers.find(s => s.id === id)?.name ?? id;
  let understood = false, insights = false;
  for (const e of events) {
    if (handBack.has(e.state)) continue;
    if (!understood && e.state === 'DISCOVERING' && run.request_interpretation?.summary) {
      understood = true;
      steps.push({ id: e.id + ':u', title: 'What I understood', detail: String(run.request_interpretation.summary), thought: true });
    }
    steps.push({ id: e.id, title: STEP[e.state] ?? e.state, detail: humanize(run, e) });
    if (!insights && e.message.startsWith('Gemini provider evidence extraction') && run.supplier_insights.length) {
      insights = true;
      for (const [i, s] of run.supplier_insights.entries())
        steps.push({ id: `${e.id}:i${i}`, title: `Notes on ${name(String(s.supplier_id ?? ''))}`, detail: String(s.summary ?? ''), thought: true });
    }
  }
  return steps;
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return <button className="icon-btn" aria-label="Copy" onClick={() => { void navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }); }}>{done ? <Check size={14}/> : <Copy size={14}/>}</button>;
}

function Thinking({ steps, live, seconds }: { steps: Step[]; live: boolean; seconds: number }) {
  const [open, setOpen] = useState<boolean | null>(null);
  const expanded = open ?? live;
  const current = steps[steps.length - 1];
  return <div className="thinking">
    <button className="thinking-head" onClick={() => setOpen(!expanded)}>
      {live ? <LoaderCircle size={15} className="spin"/> : <Sparkles size={15}/>}
      <span className={live ? 'shimmer' : ''}>{live ? (current?.title ?? 'Starting') + '…' : `Worked through ${steps.length} step${steps.length === 1 ? '' : 's'}${seconds ? ` in ${seconds}s` : ''}`}</span>
      {expanded ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}
    </button>
    {expanded && <ol className="trace">{steps.map((s, i) => <li key={s.id} className={s.thought ? 'thought' : ''}>
      <span className="dot">{live && i === steps.length - 1 ? <LoaderCircle size={11} className="spin"/> : null}</span>
      <div><strong>{s.title}</strong><p>{s.detail}</p></div></li>)}</ol>}
  </div>;
}

const CHECK_NAMES: Record<string, string> = {
  identity: 'Real company', location: 'In the requested region', factory: 'Factory, not a trader', stable: 'Established and stable',
  complaints_clear: 'No complaints or bad news', authorized: 'Authorized distributor', profile: 'Company profile', compliance: 'Compliance screen',
};
const PLAIN_BLOCKERS: Record<string, string> = {
  COST_POLICY_UNCONFIRMED: 'Duty, tax or shipping terms are not confirmed',
  NO_ELIGIBLE_SUPPLIER_OR_INSUFFICIENT_SHORTLIST: 'No supplier passed the checks, or too few for a shortlist.',
  PROVIDER_UNAVAILABLE: 'A data provider could not be reached within the budget.',
  MODEL_RESPONSE_INVALID: "Gemini's answer could not be validated.",
  PAYMENT_INSUFFICIENT_FUNDS: 'The simulated wallet does not have enough funds.',
};
const checkName = (kind: string) => CHECK_NAMES[kind] ?? kind.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const isUrl = (v: string) => /^https?:\/\//.test(v);

function Result({ result }: { result: string }) {
  if (result === 'pass') return <span className="chip ok"><Check size={12}/>Passed</span>;
  if (result === 'fail') return <span className="chip bad"><X size={12}/>Failed</span>;
  if (result === 'not_applicable') return <span className="chip unknown">Not needed</span>;
  return <span className="chip unknown"><CircleHelp size={12}/>Not confirmed</span>;
}

/** Every supplier with its checks, the evidence behind each check, and Gemini's notes. */
function EvidencePanel({ run }: { run: Run }) {
  const selected = new Set(run.decisions.flatMap(d => d.selected_supplier_ids));
  const suppliers = [...run.suppliers].sort((a, b) => Number(selected.has(b.id)) - Number(selected.has(a.id)));
  const evidence = (id: string) => run.evidence.find(e => e.id === id);
  if (!suppliers.length) return <p className="muted">No suppliers were found, so there is no evidence to show.</p>;
  return <div className="evidence-panel">
    {run.suppliers.some(s => s.profile && Object.keys(s.profile).length) && <TrustCards run={run}/>}
    {suppliers.map(s => {
      const checks = run.checks.filter(c => c.supplier_id === s.id);
      const insight = run.supplier_insights.find(i => String(i.supplier_id ?? '') === s.id);
      const used = new Set(checks.flatMap(c => c.evidence_ids));
      const other = run.evidence.filter(e => !used.has(e.id) && (e.reference.includes(s.id) || e.reference.includes(s.domain) || run.quotes.some(q => q.supplier_id === s.id && e.reference.includes(q.id))));
      return <section className="ev-supplier" key={s.id}>
        <div className="ev-head"><strong>{s.name}</strong>{selected.has(s.id) ? <span className="tag green">selected</span> : <span className="tag">not selected</span>}</div>
        <p className="ev-meta">{[s.kind, s.domain, s.locations.join(', '), s.identity === 'resolved' ? 'identity resolved' : 'identity unverified'].filter(Boolean).join(' · ')}</p>
        {insight?.summary ? <p className="ev-insight"><Sparkles size={12}/> {String(insight.summary)}</p> : null}
        {checks.length ? <ul className="ev-checks">{checks.map((c, i) => <li key={i}>
          <div className="ev-check"><span>{checkName(c.kind)}</span><Result result={c.result}/></div>
          {c.reason && <small>{c.reason}</small>}
          {c.evidence_ids.map(id => { const e = evidence(id); return e && <div className="ev-item" key={id}>
            <span className="ev-src">{e.provider.replaceAll('_', ' ')} · {e.mode === 'live' ? 'live' : 'sample'} · {e.status} · {new Date(e.observed_at).toLocaleDateString('en-IN', { dateStyle: 'medium' })}</span>
            <span>{e.fact}: <strong>{e.value}</strong></span>
            {isUrl(e.reference) ? <a href={e.reference} target="_blank" rel="noreferrer">{e.reference} <ExternalLink size={11}/></a> : <code>{e.reference}</code>}
          </div>; })}
        </li>)}</ul> : <p className="muted">No checks were recorded for this supplier.</p>}
        {other.length > 0 && <details className="fold"><summary>Other evidence ({other.length})</summary>{other.map(e => <div className="ev-item" key={e.id}><span className="ev-src">{e.provider.replaceAll('_', ' ')} · {e.status}</span><span>{e.fact}: <strong>{e.value}</strong></span></div>)}</details>}
      </section>;
    })}
  </div>;
}

function Answer({ run }: { run: Run }) {
  const name = (id: string) => run.suppliers.find(s => s.id === id)?.name ?? id;
  const spec = (id: string) => run.specs.find(s => s.id === id)?.description ?? id;
  if (run.state === 'CANCELLED') return <div className="answer"><p>Okay, I've stopped this request. Nothing was sent or paid.</p></div>;
  if (run.state === 'FAILED') return <div className="answer"><p className="bad"><CircleAlert size={15}/> I couldn't finish this request.</p><p>{run.error}</p></div>;
  const decision = run.decisions[0];
  const credits = (run.provider_usage as Record<string, unknown>[]).reduce((t, r) => t + Number(r.charged ?? r.reserved ?? 0), 0);
  const planned = run.orders.filter(o => o.state === 'PLANNED');
  return <div className="answer">
    {run.request.request_type === 'shortlist' ? <>
      {decision?.selected_supplier_ids.length ? <>
        <p>I shortlisted <strong>{decision.selected_supplier_ids.length}</strong> of the {run.suppliers.length} companies I found{run.rfqs.length ? ' and drafted a first email to each' : ''}:</p>
        <ul>{decision.selected_supplier_ids.map(id => { const c = (run.suppliers.find(s => s.id === id)?.profile as Record<string, any> | undefined)?.compliance; return <li key={id}><strong>{name(id)}</strong>{c?.verdict === 'hold' ? <span className="tag amber">held for review</span> : c?.verdict === 'approve' ? <span className="tag green">compliance passed</span> : null}{c?.verdict === 'hold' && <small>{(c.reasons as string[]).join(' · ')}</small>}</li>; })}</ul>
      </> : <p>{decision?.explanation ?? 'I could not build a shortlist from the evidence I found.'}</p>}
      {decision && decision.candidates.some(c => !decision.selected_supplier_ids.includes(c.supplier_id)) && <details className="fold"><summary>Why the others were left out</summary><ul>{decision.candidates.filter(c => !decision.selected_supplier_ids.includes(c.supplier_id)).map(c => <li key={c.quote_id}><strong>{name(c.supplier_id)}</strong>: {plain(c.reason_codes).join(', ') || 'lower ranked'}</li>)}</ul></details>}
      {run.rfqs.map(r => <div className="rfq" key={r.id}><div className="rfq-head"><Mail size={14}/><span>{name(r.supplier_id)}{r.recipient ? ` · ${r.recipient}` : ''}</span><span className="tag">draft, not sent</span><CopyButton text={`Subject: ${r.subject}\n\n${r.body}`}/></div><strong>{r.subject}</strong><p className="rfq-body">{r.body}</p></div>)}
    </> : <>
      {run.decisions.length ? <><p>Here's the plan for each item:</p><ul>{run.decisions.map(d => { const best = d.candidates.find(c => d.selected_supplier_ids.includes(c.supplier_id)); return <li key={d.spec_id}><strong>{spec(d.spec_id)}</strong>: {best ? <>{name(best.supplier_id)}{best.cost ? `, ${money(best.cost.total_minor)} landed` : ''}</> : <span className="muted">{d.explanation}</span>}</li>; })}</ul></> : <p>I couldn't put together a plan for this request.</p>}
      {run.orders.length > 0 && <p>Orders (simulated payments): {run.orders.map(o => `${name(o.supplier_id)} ${money(o.amount_minor)} ${o.state.toLowerCase()}`).join(' · ')}. Total {money(run.orders.reduce((s, o) => s + o.amount_minor, 0))}.</p>}
    </>}
    {run.state === 'WAITING_FOR_APPROVAL' && planned.length > 0 && <p className="ask">{planned.length === 1 ? 'This order is' : 'These orders are'} above your approval limit ({planned.map(o => `${name(o.supplier_id)} ${money(o.amount_minor)}`).join(', ')}). Reply <strong>approve</strong> to place {planned.length === 1 ? 'it' : 'them'}. Payment is simulated.</p>}
    {run.state === 'COMPLETED_WITH_GAPS' && <p className="muted">Some facts couldn't be confirmed, so the result has gaps. The steps above show where.</p>}
    {run.request.mode === 'live' && <p className="muted">Spent {credits.toFixed(2)} of {String(run.request.provider_credit_limit)} Crustdata credits.</p>}
    {run.suppliers.length > 0 && <details className="fold"><summary>Supplier checks and evidence ({run.suppliers.length} supplier{run.suppliers.length === 1 ? '' : 's'}, {run.checks.length} checks)</summary><EvidencePanel run={run}/></details>}
  </div>;
}

// Company records often list every nearby post office; keep the first part plus state and country.
const place = (address: string) => { const parts = address.split(',').map(x => x.trim()).filter(Boolean); return parts.length > 3 ? [parts[0], ...parts.slice(-2)].join(', ') : parts.join(', '); };

/** What a live run found when it cannot finish on verified evidence alone. */
function HandBack({ run, task }: { run: Run; task: Run['human_reviews'][number] }) {
  const credits = (run.provider_usage as Record<string, unknown>[]).reduce((t, r) => t + Number(r.charged ?? r.reserved ?? 0), 0);
  const insight = (id: string) => run.supplier_insights.find(i => String(i.supplier_id ?? '') === id);
  const why = [...new Set(task.blockers.map(b => /^[A-Z0-9_]+$/.test(b) ? (PLAIN_BLOCKERS[b] ?? plain([b])[0]) : b))];
  const items = run.specs.map(s => `${s.description}${s.quantity ? ` × ${s.quantity.toLocaleString('en-IN')}` : ''}`);
  const groups = run.specs.map((sp, k) => ({ id: sp.id, title: items[k],
    suppliers: run.suppliers.filter(s => run.quotes.some(q => q.spec_id === sp.id && q.supplier_id === s.id)) }));
  const loose = run.suppliers.filter(s => !groups.some(g => g.suppliers.includes(s)));
  if (loose.length) groups.push({ id: 'other', title: 'Other leads', suppliers: loose });
  return <div className="answer">
    <p>I couldn't verify enough to {run.request.request_type === 'purchase' ? 'place these orders' : 'build a shortlist'} yet{items.length ? <> for {items.join(', ')}</> : null}.</p>
    {run.suppliers.length ? <>
      <p>Here's what I found with live company data{run.request.mode === 'live' ? ` (${credits.toFixed(2)} Crustdata credits)` : ''}:</p>
      {groups.map(g => <div className="found-item" key={g.id}><h4>{g.title}</h4>{g.suppliers.length ? <ul>{g.suppliers.map(s => { const i = insight(s.id); return <li key={s.id}><strong>{s.name}</strong>
        {s.domain && s.domain !== 'unknown' && <> · <a href={`https://${s.domain}`} target="_blank" rel="noreferrer">{s.domain}</a></>}
        {s.locations[0] && <> · <span className="muted">{place(s.locations[0])}</span></>}
        {s.kind !== 'unknown' && <span className="tag">{s.kind}</span>}
        {i?.summary ? <small>{String(i.summary)}</small> : null}</li>; })}</ul> : <p className="muted">No companies found for this item.</p>}</div>)}
    </> : <p>Live search did not return companies that match these items.</p>}
    {run.product_page_readings.length > 0 && <details className="fold"><summary>Details read from supplier product pages ({run.product_page_readings.length})</summary>
      {run.product_page_readings.map((pg, i) => <div className="ev-item" key={i}><a href={pg.url} target="_blank" rel="noreferrer">{pg.url}</a>
        {pg.details.map((d, j) => <span key={j}>{d.field.replaceAll('_', ' ')}: <strong>{d.value}</strong></span>)}</div>)}</details>}
    {why.length > 0 && <details className="fold"><summary>Why I stopped before ordering ({why.length})</summary><ul>{why.map(w => <li key={w}><small>{w}</small></li>)}</ul></details>}
    {run.suppliers.length > 0 && <details className="fold"><summary>Supplier checks and evidence ({run.suppliers.length} supplier{run.suppliers.length === 1 ? '' : 's'}, {run.checks.length} checks)</summary><EvidencePanel run={run}/></details>}
    <p className="muted">Nothing was ordered or sent. Reply with more detail (exact specs, a supplier website, a region to search) and I'll try again, ask me to "shortlist suppliers and draft RFQs" instead, or say "cancel".</p>
  </div>;
}

function AgentTurn({ run, segment }: { run: Run | undefined; segment: number }) {
  if (!run) return <div className="msg agent"><div className="avatar"><Layers3 size={15}/></div><div className="body"><div className="thinking"><span className="thinking-head"><LoaderCircle size={15} className="spin"/><span className="shimmer">Starting…</span></span></div></div></div>;
  const events = segmentEvents(run, segment);
  const askedHere = events.some(e => handBack.has(e.state));
  const isLast = segment === segmentCount(run);
  const finished = askedHere || (isLast && (terminals.has(run.state) || waiting.has(run.state)));
  const seconds = events.length > 1 ? Math.round((new Date(events[events.length - 1].time).getTime() - new Date(events[0].time).getTime()) / 1000) : 0;
  const stop = events.find(e => handBack.has(e.state));
  const task = stop?.state === 'WAITING_FOR_HUMAN' ? [...run.human_reviews].reverse().find(t => new Date(t.created_at).getTime() <= new Date(stop.time).getTime() + 2000) : undefined;
  return <div className="msg agent"><div className="avatar"><Layers3 size={15}/></div><div className="body">
    <Thinking steps={stepsFor(run, events)} live={!finished} seconds={seconds}/>
    {askedHere ? (task ? <HandBack run={run} task={task}/>
      : <div className="answer"><p>{stop?.message}</p><p className="muted">Reply below and I'll pick up from here.</p></div>) : finished ? <Answer run={run}/> : null}
  </div></div>;
}

function App() {
  const [thread, setThread] = useState<Entry[]>(() => load('sourcing-thread', []));
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [missing, setMissing] = useState<Set<string>>(new Set());
  const [text, setText] = useState(() => (document.getElementById('shell-text') as HTMLTextAreaElement | null)?.value ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [locked, setLocked] = useState(false);
  const [passcode, setPasscode] = useState('');
  const [health, setHealth] = useState<Health>({});
  const endRef = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => { void healthPromise.then(setHealth); }, []);
  useEffect(() => save('sourcing-thread', thread), [thread]);

  const runIds = [...new Set(thread.flatMap(e => e.kind === 'agent' ? [e.runId] : []))];
  const lastRunId = runIds[runIds.length - 1];
  const lastRun = lastRunId ? runs[lastRunId] : undefined;

  const refresh = useCallback(async (all = false) => {
    try {
      const ids = (all ? runIds : runIds.filter(id => !runs[id] || !terminals.has(runs[id].state))).filter(id => !missing.has(id));
      if (!ids.length) { if (!thread.length) await api('/access'); setLocked(false); return; }
      const gone: string[] = [];
      const fetched = await Promise.all(ids.map(id => api<Run>(`/runs/${id}`).catch(e => { if (e instanceof AccessDenied) throw e; if (/not found|validation error/i.test((e as Error).message)) gone.push(id); return null; })));
      if (gone.length) setMissing(m => new Set([...m, ...gone]));
      setRuns(prev => { const next = { ...prev }; for (const r of fetched) if (r) next[r.id] = r; return next; });
      setLocked(false);
    } catch (e) { if (e instanceof AccessDenied) setLocked(true); else setError((e as Error).message); }
  }, [runIds.join(','), runs, thread.length, missing]);

  useEffect(() => { void refresh(true); }, [locked]);
  const active = runIds.some(id => !missing.has(id) && (!runs[id] || !(terminals.has(runs[id].state) || waiting.has(runs[id].state))));
  useEffect(() => { if (!active) return; const t = setInterval(() => void refresh(), 1000); return () => clearInterval(t); }, [active, refresh]);

  // Follow the conversation unless the reader has scrolled up to read something.
  useEffect(() => { if (stick.current) endRef.current?.scrollIntoView({ block: 'end' }); }, [thread, runs]);

  async function send() {
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true); setError(''); setText(''); stick.current = true;
    const add = (...entries: Entry[]) => setThread(t => [...t, ...entries]);
    try {
      if (lastRun?.state === 'WAITING_FOR_CLARIFICATION') {
        add({ kind: 'user', text: message });
        const r = await api<Run>(`/runs/${lastRun.id}/clarifications`, 'POST', { answer: message });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'agent', runId: r.id, segment: segmentCount(r) });
      } else if (lastRun?.state === 'WAITING_FOR_HUMAN') {
        add({ kind: 'user', text: message });
        const task = lastRun.human_reviews.find(t => t.status === 'pending');
        if (!task) throw new Error('There is no open question on this request. Start a new request instead.');
        const cancel = cancelWords.test(message);
        const original = lastRun.request.text;
        const r = await api<Run>(`/runs/${lastRun.id}/human-reviews/${task.id}/resolve`, 'POST', cancel
          ? { reviewer: 'chat user', action: 'cancel', note: `Cancelled from chat: ${message}`.slice(0, 4000) }
          : { reviewer: 'chat user', action: 'retry', note: `More detail from chat: ${message}`.slice(0, 4000), request_text: `${original}\nMore detail: ${message}`.slice(0, 10000) });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'agent', runId: r.id, segment: segmentCount(r) });
      } else if (lastRun?.state === 'WAITING_FOR_APPROVAL' && approveWords.test(message)) {
        add({ kind: 'user', text: message });
        let r = lastRun;
        for (const o of lastRun.orders.filter(o => o.state === 'PLANNED'))
          r = await api<Run>(`/runs/${lastRun.id}/approvals`, 'POST', { plan_version: o.plan_version, order_id: o.id, amount_minor: o.amount_minor });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'note', text: 'Approved. Placing the simulated order…' });
      } else {
        add({ kind: 'user', text: message });
        const previous = lastRun?.request;
        // A long or self-contained message is a new request; a short one builds on the last.
        const standalone = !previous || message.length > 140 || leatherWords.test(message) || electronicsWords.test(message);
        const leather = standalone ? leatherWords.test(message) : previous!.category === 'leather_goods';
        const electronics = !leather && (standalone ? electronicsWords.test(message) : previous!.category === 'electronics');
        const original = previous ? previous.text.split(/\n(?:Clarification|More detail):/)[0] : '';
        const requestText = standalone ? message : `${message}\n\nEarlier in this conversation I asked: ${original}`;
        // Every request runs on live data when a Crustdata key is configured. Gemini adopts the
        // destination and request type stated in the text; budgets and approval come from readTerms.
        const ready = await healthPromise;
        const mode = ready.crustdata_configured ? 'live' : 'fixture';
        const terms = readTerms(requestText);
        const r = await api<Run>('/runs', 'POST', { text: requestText, category: leather ? 'leather_goods' : electronics ? 'electronics' : 'general', request_type: leather ? 'shortlist' : 'purchase',
          destination: leather ? 'Uttar Pradesh, India' : 'India', ...terms,
          mode, scenario: 'normal', specs: [], llm_enabled: !!ready.gemini_configured, provider_credit_limit: 1000 });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'agent', runId: r.id, segment: 0 });
      }
    } catch (e) {
      if (e instanceof AccessDenied) setLocked(true);
      else add({ kind: 'note', text: (e as Error).message, error: true });
    } finally { setBusy(false); }
  }

  if (locked) return <div className="gate"><form className="card gate-card" onSubmit={e => { e.preventDefault(); setAccessCode(passcode.trim()); setPasscode(''); setLocked(false); }}>
    <span className="brand-icon"><Layers3 size={23}/></span><h1>Sourcing Desk</h1><p className="muted">Enter the team passcode to continue.</p>
    <label>Passcode<input autoFocus required type="password" autoComplete="current-password" value={passcode} onChange={e => setPasscode(e.target.value)}/></label>
    <button className="primary">Unlock</button></form></div>;

  const placeholder = lastRun?.state === 'WAITING_FOR_CLARIFICATION' ? 'Answer the question above…'
    : lastRun?.state === 'WAITING_FOR_HUMAN' ? 'Add the missing detail, or say "cancel"…'
    : lastRun?.state === 'WAITING_FOR_APPROVAL' ? 'Reply "approve", or ask something else…'
    : thread.length ? 'Ask a follow-up or start a new request…' : 'What do you need to source? Include specs, quantities and where it should go.';
  const prompt = <form className="prompt" onSubmit={e => { e.preventDefault(); void send(); }}>
    <textarea aria-label="Message" rows={thread.length ? 1 : 4} value={text} placeholder={placeholder} autoFocus
      onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}/>
    <button className="send" aria-label="Send" disabled={busy || !text.trim()}>{busy ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={18}/>}</button>
  </form>;

  if (!thread.length) return <div className="home">
    <div className="home-inner">
      <div className="home-brand"><span className="brand-icon"><Layers3 size={22}/></span>Sourcing Desk</div>
      <h1>What do you need to source?</h1>
      {prompt}
      {error && <div role="alert" className="error">{error}</div>}
      <div className="examples">{examples.map(x => <button key={x} type="button" onClick={() => setText(x)}>{x}</button>)}</div>
      <p className="muted center"><ShieldCheck size={13}/> Payments are simulated and RFQs stay drafts. Nothing is sent to suppliers.</p>
    </div>
  </div>;

  return <div className="chat">
    <header className="chat-top"><span className="home-brand"><span className="brand-icon"><Layers3 size={16}/></span>Sourcing Desk</span>
      <button className="secondary" onClick={() => { setThread([]); setRuns({}); setMissing(new Set()); setText(''); }}><Plus size={15}/>New chat</button></header>
    <div className="chat-scroll" ref={scroller} onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
      <div className="chat-col">
        {thread.map((e, i) => e.kind === 'user' ? <div className="msg user" key={i}><div className="bubble">{e.text}</div></div>
          : e.kind === 'note' ? <div className={`note ${e.error ? 'bad' : ''}`} key={i}>{e.error && <CircleAlert size={14}/>}{e.text}</div>
          : missing.has(e.runId) ? <div className="note" key={i}>This reply is no longer available on the server. Start a new chat to continue.</div>
          : <AgentTurn key={i} run={runs[e.runId]} segment={e.segment}/>)}
        {error && <div className="note bad"><CircleAlert size={14}/>{error}</div>}
        <div ref={endRef}/>
      </div>
    </div>
    <div className="composer"><div className="chat-col">{prompt}<p className="fine">Payments are simulated and RFQs stay drafts. Nothing is sent to suppliers.</p></div></div>
  </div>;
}

// index.html paints a static home page immediately. Mount once the stylesheet is in,
// then swap: whatever was typed into the static prompt carries over.
function stylesheetReady(): Promise<void> {
  const link = document.getElementById('app-css') as HTMLLinkElement | null;
  if (!link || link.sheet) return Promise.resolve();
  return new Promise(resolve => {
    const done = () => resolve();
    link.addEventListener('load', done, { once: true });
    link.addEventListener('error', done, { once: true });
    setTimeout(done, 10000);
  });
}

void stylesheetReady().then(() => {
  createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
  requestAnimationFrame(() => document.getElementById('shell')?.remove());
});
