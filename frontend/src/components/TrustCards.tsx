import { Check, CircleHelp, Mail, X } from 'lucide-react';
import type { Run } from '../types';

type Supplier = Run['suppliers'][number];
type Check = Run['checks'][number];

const when = (value: string) => new Date(value).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium' });

export const PLAIN_REASONS: Record<string, string> = {
  IDENTITY_UNVERIFIED: 'No matching company record',
  SUPPLIER_TYPE_NOT_ELIGIBLE: 'Not a manufacturer',
  SPEC_MISMATCH_OR_UNVERIFIED: 'Wrong region or product, or not confirmed',
  DESTINATION_UNVERIFIED_OR_UNSERVED: 'Delivery to you not confirmed',
  EVIDENCE_STALE: 'Data is out of date',
  QUOTE_UNVERIFIED: 'Facts not checked yet',
  MOQ_NOT_MET: 'Order below minimum quantity',
  AUTHORIZED_UNVERIFIED_OR_FAILED: 'Not a confirmed authorized distributor',
  FACTORY_UNVERIFIED_OR_FAILED: 'Not proven to be a factory',
  STOCK_UNVERIFIED: 'Stock not confirmed',
  INSUFFICIENT_STOCK: 'Not enough stock',
  DELIVERY_UNVERIFIED: 'Delivery date not confirmed',
  DEADLINE_MISSED: 'Cannot meet the deadline',
  LEAD_TIME_UNVERIFIED: 'Lead time not confirmed',
  COST_INCOMPLETE: 'Price incomplete',
  BELOW_RELIABILITY_THRESHOLD: 'Reliability below the bar',
  STABLE_FAILED: 'Too new, closed or shrinking fast',
  COMPLAINTS_CLEAR_FAILED: 'Red flag in the news',
  COMPLIANCE_FAILED: 'Blocked by the compliance screen',
  AUTHORIZED_FAILED: 'Not an authorized distributor',
};
export const plain = (codes: string[]) => codes.map(c => PLAIN_REASONS[c] ?? c.toLowerCase().replaceAll('_', ' '));

const ROWS: [string, string][] = [
  ['identity', 'Real company'],
  ['location', 'In the requested region'],
  ['factory', 'Factory, not a trader'],
  ['stable', 'Established and stable'],
  ['complaints_clear', 'No complaints or bad news'],
];

function Chip({ result }: { result: Check['result'] | undefined }) {
  if (result === 'pass') return <span className="chip ok"><Check size={13}/>Checked</span>;
  if (result === 'fail') return <span className="chip bad"><X size={13}/>Failed</span>;
  return <span className="chip unknown"><CircleHelp size={13}/>Not confirmed</span>;
}

export function TrustCards({ run }: { run: Run }) {
  const decision = run.decisions[0];
  if (!run.suppliers.some(s => s.profile && Object.keys(s.profile).length) && !run.checks.some(c => c.kind === 'profile')) return null;
  const selected = new Set(decision?.selected_supplier_ids ?? []);
  const rank = (s: Supplier) => {
    if (selected.has(s.id)) return 0;
    const profile = run.checks.find(c => c.supplier_id === s.id && c.kind === 'profile');
    if (profile && profile.result !== 'pass') return 3;
    const cand = decision?.candidates.find(c => c.supplier_id === s.id);
    return cand?.eligible ? 1 : 2;
  };
  const suppliers = [...run.suppliers].sort((a, b) => rank(a) - rank(b));
  return <section className="trust-section">
    <div className="spread"><div><h2>Supplier trust cards</h2><p className="muted">Every fact comes from Crustdata's company graph, with its source and date. Anything we could not check is shown as not confirmed, never assumed.</p></div></div>
    <div className="trust-grid">{suppliers.map(s => {
      const checks = run.checks.filter(c => c.supplier_id === s.id);
      const profileCheck = checks.find(c => c.kind === 'profile');
      const candidate = decision?.candidates.find(c => c.supplier_id === s.id);
      const p = (s.profile ?? {}) as Record<string, any>;
      const verdict = p.compliance?.verdict;
      const status = selected.has(s.id) && verdict === 'hold' ? ['Shortlist · review', 'amber'] : selected.has(s.id) ? ['On shortlist', 'green'] : verdict === 'block' ? ['Blocked', 'red'] : profileCheck && profileCheck.result !== 'pass' ? ['Not checked', ''] : candidate?.eligible ? ['Qualified alternative', 'green'] : ['Rejected', 'red'];
      const evidence = (ids: string[]) => ids.map(id => run.evidence.find(e => e.id === id)).filter(Boolean)[0];
      return <article className={`card trust-card ${status[1]}`} key={s.id}>
        <div className="spread"><div><h3>{s.name}</h3><small className="muted">{[p.headquarters || s.locations[0], s.domain !== 'unknown' ? s.domain : null].filter(Boolean).join(' · ')}</small></div><span className={`pill ${status[1]}`}>{status[0]}</span></div>
        {s.aliases.length > 0 && <small className="muted">Also listed as: {s.aliases.join(', ')}</small>}
        {profileCheck && profileCheck.result !== 'pass' ? <p className="muted">{profileCheck.reason}</p> : <>
          <div className="facts">{[p.founded && `Founded ${p.founded}`, p.headcount && `${p.headcount} staff`, typeof p.headcount_change_6m === 'number' && `${p.headcount_change_6m >= 0 ? '+' : ''}${Math.round(p.headcount_change_6m)}% in 6 months`, p.naics && `NAICS ${p.naics}`].filter(Boolean).map(f => <span className="fact" key={String(f)}>{f}</span>)}</div>
          <ul className="trust-rows">{ROWS.map(([kind, label]) => { const c = checks.find(x => x.kind === kind); const e = c && evidence(c.evidence_ids); return <li key={kind}><div className="spread"><span>{label}</span><Chip result={c?.result}/></div>{c && <small>{c.reason}</small>}{e && <small className="source">Source: {e.provider.replaceAll('_', ' ')} · {when(e.observed_at)}</small>}</li>; })}</ul>
          {p.compliance && <ComplianceBlock c={p.compliance}/>}
          {candidate && !candidate.eligible && <p className="reject"><strong>Why not:</strong> {plain(candidate.reason_codes).join(' · ')}</p>}
          {candidate && <small className="muted">Reliability {candidate.reliability}/100 · {candidate.coverage}% of signals checked</small>}
          {s.contact?.name && selected.has(s.id) && <p className="contact"><Mail size={14}/>RFQ goes to {s.contact.name}{s.contact.title ? `, ${s.contact.title}` : ''}</p>}
        </>}
      </article>; })}</div>
  </section>;
}

export function CreditLedger({ run, onLower, busy }: { run: Run; onLower: (credits: number) => void; busy: boolean }) {
  const rows = run.provider_usage as Record<string, any>[];
  const spent = rows.reduce((sum, r) => sum + Number(r.charged ?? r.reserved ?? 0), 0);
  const ceiling = Number(run.request.provider_credit_limit);
  return <section className="card">
    <div className="spread"><div><h2>Crustdata credits</h2><p>Real data spend. Every call says why it was made.</p></div><span className="pill">{spent.toFixed(2)} / {ceiling} credits</span></div>
    <div className="meter"><span style={{ width: `${Math.min(100, ceiling ? (spent / ceiling) * 100 : 100)}%` }}/></div>
    <form className="budget-form" onSubmit={e => { e.preventDefault(); const v = Number(new FormData(e.currentTarget).get('credits')); if (!Number.isNaN(v)) onLower(v); }}>
      <label>Credit ceiling<input name="credits" type="number" min="0" step="0.5" placeholder={String(ceiling)} required/></label>
      <button className="secondary" disabled={busy}>Update ceiling</button>
    </form>
    {rows.length ? rows.map(r => <div className="ledger-row" key={String(r.id)}><div>{String(r.purpose || r.path)}<small>{String(r.path)} · {String(r.state)}{r.created_at ? ` · ${when(String(r.created_at))}` : ''}</small></div><strong>{Number(r.charged ?? r.reserved ?? 0).toFixed(2)}</strong></div>) : <p className="muted">No Crustdata calls yet.</p>}
  </section>;
}

function ComplianceBlock({ c }: { c: Record<string, any> }) {
  const label: Record<string, [string, string]> = { approve: ['Approved', 'ok'], hold: ['Hold for review', 'unknown'], block: ['Blocked', 'bad'] };
  const [text, cls] = label[c.verdict] ?? ['Not screened', 'unknown'];
  const directors = (c.directors ?? []) as Record<string, any>[];
  return <div className="compliance">
    <div className="spread"><strong>Compliance screen</strong><span className={`chip ${cls}`}>{text}</span></div>
    {(c.reasons as string[]).map(r => <small key={r}>{r}</small>)}
    {directors.map(d => <div className="director" key={d.name}>
      <small><strong>{d.name}</strong>{d.title ? `, ${d.title}` : ''} · found via {d.source}</small>
      {(d.adverse ?? []).map((a: any) => <small className="bad-text" key={a.title}>Adverse: {a.title}</small>)}
      {(d.namesakes_cleared ?? []).length > 0 && <small>Cleared {d.namesakes_cleared.length} namesake article{d.namesakes_cleared.length === 1 ? '' : 's'}: {d.namesakes_cleared.slice(0, 2).map((a: any) => `"${a.title}"`).join(', ')}</small>}
      {d.adverse !== undefined && !(d.adverse ?? []).length && !(d.namesakes_cleared ?? []).length && <small>No news about this person in the last 3 years</small>}
    </div>)}
  </div>;
}
