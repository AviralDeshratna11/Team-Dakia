import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, Box, Check, ChevronDown, CircleHelp, ClipboardList, FileText, Layers3, LoaderCircle, Plus, Search, ShieldCheck, Wallet } from 'lucide-react';
import { api } from './api/client';
import type { Run, Ledger, Reservation, RequestBody, Spec } from './types';
import './styles.css';
import { LLMInsights } from './components/LLMInsights';

const money = (minor: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(minor / 100);
const date = (value: string) => new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' });
const electronics = 'Source ESP32-S3 modules, a 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors. 500 of each, delivered to Bengaluru within three weeks.';
const leather = 'Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft an RFQ to each.';
const stageNames = ['PARSING', 'DISCOVERING', 'VERIFYING', 'RESEARCHING', 'DECIDING', 'EXECUTING'];
const terminals = new Set(['COMPLETED', 'COMPLETED_WITH_GAPS', 'FAILED']);

function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [selected, setSelected] = useState<string | null>(() => localStorage.getItem('sourcing-run'));
  const [tab, setTab] = useState('Plan');
  const [ledger, setLedger] = useState<Ledger[]>([]);
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [category, setCategory] = useState<'electronics' | 'leather_goods'>('electronics');
  const [text, setText] = useState(electronics);
  const [destination, setDestination] = useState('Bengaluru, India');
  const [deadline, setDeadline] = useState(() => new Date(Date.now() + 21 * 86400000).toISOString().slice(0, 10));
  const [budget, setBudget] = useState('2000');
  const [threshold, setThreshold] = useState('100000');
  const [scenario, setScenario] = useState<RequestBody['scenario']>('normal');
  const [mode, setMode] = useState<'fixture' | 'live'>('fixture');
  const [structured, setStructured] = useState('');
  const [llmEnabled, setLlmEnabled] = useState(false);
  const [answer, setAnswer] = useState('');
  const [budgetEdit, setBudgetEdit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    try {
      const list = await api<Run[]>('/runs');
      if (generation.current !== current) return;
      setRuns(list); setConnected(true);
      if (selected) {
        const [r, l, b] = await Promise.all([api<Run>(`/runs/${selected}`), api<Ledger[]>(`/runs/${selected}/ledger`), api<Reservation[]>(`/runs/${selected}/reservations`)]);
        if (generation.current !== current) return;
        setRun(r); setLedger(l); setReservations(b);
      }
    } catch (e) { if (generation.current === current) { setConnected(false); setError((e as Error).message); } }
  }, [selected]);

  useEffect(() => { void refresh(); const timer = setInterval(() => void refresh(), 1200); return () => { clearInterval(timer); generation.current++; }; }, [refresh]);
  useEffect(() => { if (selected) localStorage.setItem('sourcing-run', selected); else localStorage.removeItem('sourcing-run'); }, [selected]);

  async function action(work: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await work(); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  function pick(id: string | null) { generation.current++; setSelected(id); setRun(null); setLedger([]); setReservations([]); setTab('Plan'); setError(''); setAnswer(''); setBudgetEdit(''); }
  async function create() {
    await action(async () => {
      const specs: Spec[] = structured.trim() ? JSON.parse(structured) : [];
      const result = await api<Run>('/runs', 'POST', { text, category, request_type: category === 'electronics' ? 'purchase' : 'shortlist', destination,
        deadline: new Date(`${deadline}T23:59:00+05:30`).toISOString(), research_budget_minor: Math.round(Number(budget) * 100),
        approval_threshold_minor: Math.round(Number(threshold) * 100), mode, scenario, specs, llm_enabled: llmEnabled, ...(mode === 'live' ? { supplier_domains: ['mouser.com'] } : {}) });
      pick(result.id);
    });
  }
  const spent = ledger.filter(l => l.account === 'research').reduce((s, l) => s + l.amount_minor, 0);
  const held = reservations.filter(r => r.status === 'reserved').reduce((s, r) => s + r.amount_minor, 0);
  const supplierName = (id: string) => run?.suppliers.find(s => s.id === id)?.name ?? id;
  const selectedTotal = run?.orders.reduce((s, o) => s + o.amount_minor, 0) ?? 0;

  return <div className="shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="Sourcing Desk home"><span className="brand-icon"><Layers3 size={23}/></span><span>Sourcing<span className="brand-light">Desk</span><small>PROCUREMENT WORKSPACE</small></span></a>
      <div className="workspace"><span className="workspace-logo">S</span><div>Founder workspace<small>Local development</small></div><ChevronDown size={15}/></div>
      <div className="nav-label">WORKSPACE</div>
      <button className="nav active" onClick={() => setTab('Plan')}><ClipboardList size={18}/>Sourcing requests<span>{runs.length}</span></button>
      <button className="nav" disabled={!run} onClick={() => setTab('Research')}><Wallet size={18}/>Research & spending</button>
      <button className="nav" disabled={!run} onClick={() => setTab('Orders & RFQs')}><Box size={18}/>Orders & RFQs</button>
      <div className="recent-title">RECENT REQUESTS <button aria-label="New request" onClick={() => pick(null)}><Plus size={16}/></button></div>
      <div className="recent-list">{runs.slice(0, 8).map(r => <button key={r.id} className={`recent ${selected === r.id ? 'chosen' : ''}`} onClick={() => pick(r.id)}><span>{r.request.category === 'electronics' ? 'Electronics BOM' : 'Leather manufacturers'}</span><small>{r.state.replaceAll('_', ' ').toLowerCase()}</small></button>)}{!runs.length && <p className="muted px">Your requests will appear here.</p>}</div>
      <div className="sidebar-foot"><ShieldCheck size={18}/><div>Simulation environment<small>Payments are always mocked.</small></div></div>
    </aside>
    <main>
      <header className="topbar"><span>Workspace <span className="slash">/</span> Sourcing requests</span><span className={`connection ${connected ? '' : 'offline'}`}><span/>{connected ? 'API connected' : 'API unavailable'}</span></header>
      <div className="content">
        <div className="page-heading"><div><div className="eyebrow">YOUR SOURCING OPERATIONS</div><h1>{selected ? 'Request overview' : 'Source with confidence.'}</h1><p>{selected ? 'Every supplier, decision and rupee. In one place.' : 'From a buying request to a supplier plan you can inspect.'}</p></div>{selected && <button className="primary" onClick={() => pick(null)}><Plus size={17}/>New request</button>}</div>
        <div className="environment"><ShieldCheck size={18}/><span><strong>Hackathon sandbox</strong> · Fixture or live identity evidence. Payments stay simulated and RFQs stay drafts.</span><span className="pill">LOCAL ONLY</span></div>
        {error && <div role="alert" className="error">{error}<button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
        {!selected ? <div className="create-layout"><section className="card request-card"><div className="section-heading"><span className="icon-tile"><Search size={19}/></span><div><h2>What do you need to source?</h2><p>Include exact specs and a quantity for each item.</p></div></div>
          <form onSubmit={e => { e.preventDefault(); void create(); }}>
            <div className="form-row"><label>Category<select value={category} onChange={e => { const c = e.target.value as typeof category; setCategory(c); setText(c === 'electronics' ? electronics : leather); }}><option value="electronics">Electronics</option><option value="leather_goods">Leather goods</option></select></label><label>Data mode<select value={mode} onChange={e => setMode(e.target.value as typeof mode)}><option value="fixture">Fixture · synthetic data</option><option value="live">Live - identity only (0.2-credit cap)</option></select></label></div>
            <label className="llm-option"><input type="checkbox" checked={llmEnabled} onChange={e => setLlmEnabled(e.target.checked)}/>Use Gemini to parse the request and extract supplier insights</label>
            <p className="muted">Gemini requires a worker API key. Normally two model calls; its token usage is separate from the 0.2 Crustdata-credit cap.</p>
            <label>Your sourcing request<textarea required rows={6} value={text} onChange={e => setText(e.target.value)}/></label>
            <div className="form-row"><label>Delivery destination<input required value={destination} onChange={e => setDestination(e.target.value)}/></label><label>Delivery deadline · IST<input required type="date" value={deadline} onChange={e => setDeadline(e.target.value)}/></label></div>
            <div className="form-row"><label>Research budget · INR<input required type="number" min="0" step="0.01" value={budget} onChange={e => setBudget(e.target.value)}/></label><label>Approval required above · INR<input required type="number" min="0" step="0.01" value={threshold} onChange={e => setThreshold(e.target.value)}/></label></div>
            <label>Demo scenario<select value={scenario} onChange={e => setScenario(e.target.value as typeof scenario)}><option value="normal">Normal sourcing flow</option><option value="missing_stock">Missing stock · buy mock research</option><option value="provider_timeout">Provider timeout / 429 · finish with gaps</option><option value="checkout_timeout">Checkout timeout · reconcile payment</option><option value="checkout_crash">Checkout crash · restart worker to recover</option></select></label>
            <details><summary>Structured specification editor</summary><p className="muted">Optional JSON array using the Spec contract in /docs. Unsupported fixture specifications remain unverified.</p><textarea aria-label="Structured specifications JSON" rows={5} placeholder='[{"id":"esp32","description":"ESP32-S3 module","quantity":500,"parameters":{"fixture_item":"esp32","part_number":"ESP32-S3"}}]' value={structured} onChange={e => setStructured(e.target.value)}/></details>
            <div className="form-bottom"><span><ShieldCheck size={15}/>No real money moves</span><button className="primary" disabled={busy || !connected}>{busy ? <LoaderCircle size={16} className="spin"/> : <Plus size={16}/>}Create sourcing request</button></div>
          </form></section><aside className="guide"><span className="pill green">HOW IT WORKS</span><h2>A clear path from<br/>request to result.</h2>{[['01','Understand the spec','Resolve ambiguity before comparing parts.'],['02','Verify suppliers','Check identity and category trust signals.'],['03','Make the decision','Compare eligible landed costs in plain code.'],['04','Finish the work','Mock checkout or an RFQ ready for review.']].map(([n,title,desc]) => <div className="guide-step" key={n}><span>{n}</span><div><h3>{title}</h3><p>{desc}</p></div></div>)}<div className="guide-note"><CircleHelp size={19}/><p>The worker runs separately. Requests wait safely in SQLite until it is running.</p></div></aside></div> : !run ? <div className="card empty"><LoaderCircle className="spin"/><p>Loading persisted request…</p></div> : <>
          <div className="stats"><Stat label="ORDER PLAN" value={money(selectedTotal)} sub="Estimated landed total · demo tax rates" icon={<Box size={19}/>}/><Stat label="RESEARCH SPEND" value={money(spent)} sub={`${money(held)} reserved · ${money(run.request.research_budget_minor)} ceiling`} icon={<Wallet size={19}/>}/><Stat label="SUPPLIERS CHECKED" value={String(run.suppliers.length).padStart(2, '0')} sub="Canonical identities · aliases merged" icon={<ShieldCheck size={19}/>}/><Stat label="WORKFLOW STATUS" value={run.state.replaceAll('_', ' ')} sub={run.request.mode === 'fixture' ? 'Fixture data · simulated execution' : 'Live identity evidence - 0.2-credit cap'} icon={<Activity size={19}/>}/></div>
          <section className="card progress-card"><div className="spread"><h2>{run.request.category === 'electronics' ? 'Electronics sourcing' : 'Leather-goods manufacturers'}</h2><span className="pill">{run.id.slice(0,8)}</span></div><p className="request-text">{run.request.text}</p><div className="stages">{stageNames.map((s,i) => { const visited = run.events.some(e => e.state === s); return <div key={s} className={visited ? 'visited' : ''}><span>{visited ? <Check size={14}/> : i+1}</span>{s.toLowerCase()}</div>; })}</div><small className="muted">Created {date(run.created_at)} · Worker advances one persisted step per poll. {terminals.has(run.state) ? 'Run finished.' : 'If progress stops, check the worker terminal.'}</small></section>
          {run.state === 'WAITING_FOR_CLARIFICATION' && <section className="card attention"><h2>One detail before we continue</h2><p>{run.clarification?.question}</p><form onSubmit={e => { e.preventDefault(); void action(() => api(`/runs/${run.id}/clarifications`, 'POST', { answer, ...(structured.trim() ? { specs: JSON.parse(structured) } : {}) })); }}><label>Your answer<input required value={answer} onChange={e => setAnswer(e.target.value)} placeholder="e.g. 500 of each"/></label><details><summary>Provide structured specs instead</summary><textarea aria-label="Clarified specifications JSON" rows={5} value={structured} onChange={e => setStructured(e.target.value)}/></details><button className="primary" disabled={busy}>Save and continue</button></form></section>}
          {run.error && <div className="error" role="alert">{run.error}</div>}
          <LLMInsights run={run}/>
          <div className="tabs" role="tablist" aria-label="Request details">{['Plan', 'Research', 'Orders & RFQs', 'Activity'].map(t => <button role="tab" aria-selected={tab === t} key={t} onClick={() => setTab(t)}>{t}{t === 'Orders & RFQs' && <span>{run.orders.length + run.rfqs.length}</span>}</button>)}</div>
          {tab === 'Plan' && <section>{!run.decisions.length ? <Empty title="Your plan is taking shape" text="Supplier comparisons appear after verification and research finish."/> : run.decisions.map(d => <section className="card decision" key={d.spec_id}><div className="spread"><h2>{run.specs.find(s => s.id === d.spec_id)?.description}</h2><span className={`pill ${d.outcome === 'selected' ? 'green' : 'amber'}`}>{d.outcome.replaceAll('_',' ')}</span></div><p>{d.explanation}</p>{d.alternatives.map(a => <p className="muted" key={a}>{a}</p>)}<div className="table-scroll"><table><thead><tr><th>Supplier</th><th>Reliability</th><th>Landed cost</th><th>Decision</th></tr></thead><tbody>{d.candidates.map(c => <tr key={c.quote_id}><td><strong>{supplierName(c.supplier_id)}</strong><small>{run.suppliers.find(s => s.id === c.supplier_id)?.kind} · {run.request.mode}</small></td><td><strong>{c.reliability}/100</strong><small>{c.coverage}% evidence coverage</small><details><summary>Score breakdown</summary>{Object.entries(c.reliability_breakdown).map(([k,v]) => <small key={k}>{k}: {v}</small>)}</details></td><td>{c.cost ? <><strong>{money(c.cost.total_minor)}</strong><details><summary>Cost breakdown</summary><small>Goods {money(c.cost.goods_minor)}</small><small>Shipping {money(c.cost.shipping_minor)}</small><small>Duty {money(c.cost.duty_minor)}</small><small>Tax {money(c.cost.tax_minor)}</small><small>{c.cost.assumptions}</small></details></> : <span className="muted">{run.request.request_type === 'shortlist' ? 'RFQ required' : 'Incomplete'}</span>}</td><td>{d.selected_supplier_ids.includes(c.supplier_id) ? <span className="selected"><Check size={14}/>Selected</span> : <span className="muted">{c.eligible ? 'Eligible alternative' : 'Ineligible'}</span>}<small className="reason">{c.reason_codes.join(' · ').toLowerCase().replaceAll('_',' ')}</small><details><summary>Sources & timestamps</summary>{c.evidence_ids.map(id => { const e=run.evidence.find(x => x.id === id); return e && <div className="evidence" key={id}><strong>{e.provider} · {e.mode}</strong><small>{e.status} · {e.mode} · {date(e.observed_at)}</small><code>{e.reference}</code><p>{e.fact}: {e.value}</p></div>; })}</details></td></tr>)}</tbody></table></div></section>)}</section>}
          {tab === 'Research' && <section className="card"><div className="spread"><div><h2>Research & budget</h2><p>Only purchase data that could change a decision.</p></div><span className="pill">MOCK WALLET</span></div><form className="budget-form" onSubmit={e => { e.preventDefault(); void action(() => api(`/runs/${run.id}/budget`, 'PATCH', { research_budget_minor: Math.round(Number(budgetEdit) * 100) })); }}><label>New research ceiling · INR<input required type="number" min="0" step="0.01" value={budgetEdit} placeholder={String(run.request.research_budget_minor/100)} onChange={e => setBudgetEdit(e.target.value)}/></label><button className="secondary" disabled={busy}>Update ceiling</button></form><p className="muted">Spent {money(spent)} · Reserved {money(held)} · Remaining {money(Math.max(0,run.request.research_budget_minor-spent-held))}. A reduction cannot reverse committed spending.</p>{run.research.length ? run.research.map(p => <div className="research-entry" key={p.id}><div className="spread"><h3>{p.question}</h3><span className="pill">{p.status} · {money(p.quoted_minor)}</span></div><p>{p.rationale}</p><small>Decision changed: {p.changed_decision === null || p.changed_decision === undefined ? 'not yet evaluated' : p.changed_decision ? 'yes' : 'no'}</small></div>) : <p className="muted">No research purchase proposals yet.</p>}<h3 className="ledger-title">Settled ledger</h3>{ledger.length ? ledger.map(l => <div className="ledger-row" key={l.id}><div>{l.description}<small>{l.account} · {date(l.created_at)}</small></div><strong>{money(l.amount_minor)}</strong></div>) : <p className="muted">No money has been committed.</p>}</section>}
          {tab === 'Orders & RFQs' && <section className="card"><div className="spread"><h2>Orders & RFQ drafts</h2><span className="pill">SIMULATED</span></div>{!run.orders.length && !run.rfqs.length && <p className="muted">Orders or drafts appear after a plan is selected.</p>}{run.orders.map(o => <div className="order-entry" key={o.id}><div className="spread"><div><h3>{supplierName(o.supplier_id)}</h3><p>{run.specs.find(s => s.id === o.spec_id)?.description}</p></div><strong>{money(o.amount_minor)}</strong></div><div className="spread"><span className={`pill ${o.state === 'PAID' ? 'green' : 'amber'}`}>{o.state} · mock</span>{o.state === 'PLANNED' && <button className="primary" disabled={busy} onClick={() => void action(() => api(`/runs/${run.id}/approvals`, 'POST', { plan_version: o.plan_version, order_id: o.id, amount_minor: o.amount_minor }))}>Approve {money(o.amount_minor)}</button>}</div><details><summary>Payment reference</summary><code>Idempotency key: {o.idempotency_key}</code><small>Plan version: {o.plan_version}</small><small>Mock payment: {o.payment_id ?? 'not settled'}</small></details></div>)}{run.rfqs.map(r => <div className="order-entry" key={r.id}><div className="spread"><h3>{supplierName(r.supplier_id)}</h3><span className="pill">DRAFT · UNSENT</span></div><h4>{r.subject}</h4><p>{r.body}</p></div>)}<button className="secondary" disabled={busy} onClick={() => void action(() => api(`/runs/${run.id}/resume`, 'POST'))}>Resume / reconcile saved work</button></section>}
          {tab === 'Activity' && <section className="card"><h2>Persisted activity</h2><div className="timeline">{run.events.map(e => <div className="timeline-entry" key={e.id}><span className="timeline-dot"/><div><strong>{e.state.replaceAll('_',' ')}</strong><p>{e.message}</p><small>{date(e.time)}</small></div></div>)}</div></section>}
        </>}
        <footer><span>Sourcing Desk</span><span>Evidence before decisions. Human input for exceptions.</span><a href="http://127.0.0.1:8000/docs" target="_blank" rel="noreferrer">API reference</a></footer>
      </div>
    </main>
  </div>;
}

function Stat({label,value,sub,icon}: {label:string;value:string;sub:string;icon:React.ReactNode}) { return <section className="card stat"><div>{label}{icon}</div><strong>{value}</strong><p>{sub}</p></section>; }
function Empty({title,text}: {title:string;text:string}) { return <div className="card empty"><FileText size={28}/><h2>{title}</h2><p>{text}</p></div>; }

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
