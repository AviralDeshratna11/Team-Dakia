import { useState } from 'react';
import type { Run } from '../types';
import type { components } from '../types/schema';

type Resolution = components['schemas']['HumanReviewResolution'];

export function HumanReviewInbox({ runs, onSelect }: { runs: Run[]; onSelect: (id: string) => void }) {
  const pending = runs.flatMap(run => (run.human_reviews ?? []).filter(task => task.status === 'pending').map(task => ({ run, task })));
  return <section className="card decision" aria-label="Human review inbox">
    <div className="spread"><h2>Human review inbox</h2><span className="pill amber">{pending.length} pending</span></div>
    <p>A reviewer can inspect the saved request, evidence and blockers. These are in-app tasks; no outside messages are sent.</p>
    {!pending.length && <p className="muted">No requests currently need review.</p>}
    {pending.map(({ run, task }) => <div className="order-entry" key={task.id}><div className="spread"><div><h3>{run.request.text.slice(0, 100)}</h3><p>{task.summary}</p></div><button className="secondary" onClick={() => onSelect(run.id)}>Review request</button></div></div>)}
  </section>;
}

export function HumanReviewPanel({ run, busy, onResolve }: { run: Run; busy: boolean; onResolve: (taskId: string, resolution: Resolution) => Promise<void> }) {
  const task = (run.human_reviews ?? []).find(review => review.status === 'pending');
  const [reviewer, setReviewer] = useState('');
  const [action, setAction] = useState<Resolution['action']>('retry');
  const [note, setNote] = useState('');
  const [reference, setReference] = useState('');
  const [requestText, setRequestText] = useState('');
  const [specs, setSpecs] = useState('');
  const [changeProvider, setChangeProvider] = useState(false);
  const [domain, setDomain] = useState(run.request.supplier_domains.join(', '));
  const [budget, setBudget] = useState(String(run.request.provider_credit_limit));
  const [error, setError] = useState('');
  if (!task) return (run.human_reviews ?? []).length > 0 ? <section className="card decision"><h2>Human review history</h2>{(run.human_reviews ?? []).map(review => <div className="research-entry" key={review.id}><h3>{review.resolution?.replaceAll('_', ' ')} - {review.reviewer}</h3><p>{review.note}</p>{review.reference && <code>{review.reference}</code>}</div>)}</section> : null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    try {
      const resolution: Resolution = { reviewer, action, note, reference: reference.trim() || null };
      if (action === 'retry') {
        if (requestText.trim()) resolution.request_text = requestText;
        if (specs.trim()) resolution.specs = JSON.parse(specs);
        if (changeProvider) {
          resolution.supplier_domains = domain.split(',').map(value => value.trim().toLowerCase()).filter(Boolean);
          resolution.provider_credit_limit = budget;
        }
      }
      await onResolve(task!.id, resolution);
    } catch (err) { setError((err as Error).message); }
  }

  return <section className="card attention" aria-label="Human assistance required">
    <div className="spread"><h2>Human assistance required</h2><span className="pill amber">PAUSED FOR REVIEW</span></div>
    <p>{task.summary}</p><p className="muted">Provider calls and ordering are paused. Review the evidence before deciding what happens next.</p>
    <details open><summary>Why the automated request could not finish</summary><ul className="review-blockers">{task.blockers.map(blocker => <li key={blocker}>{blocker.replaceAll('_', ' ')}</li>)}</ul></details>
    <p className="muted">Affected items: {task.spec_ids.map(id => run.specs.find(spec => spec.id === id)?.description ?? id).join('; ') || 'The entire request'}</p>
    <form onSubmit={event => void submit(event)}>
      <div className="form-row"><label>Reviewer name<input required minLength={2} value={reviewer} onChange={event => setReviewer(event.target.value)}/></label>
        <label>Decision<select value={action} onChange={event => setAction(event.target.value as Resolution['action'])}><option value="retry">Revise and retry</option><option value="manual_completion">Record manual handling</option><option value="cancel">Cancel remaining work</option></select></label></div>
      <label>Review notes<textarea required minLength={5} value={note} onChange={event => setNote(event.target.value)} placeholder="Explain the evidence checked, changes requested, or how the items were handled."/></label>
      {action === 'manual_completion' && <><label>Completion reference<input required value={reference} onChange={event => setReference(event.target.value)} placeholder="Receipt, order reference, RFQ record or procurement ticket"/></label><p className="muted">Records the reviewer's report for the affected items. It does not create an automated payment or independently verify a purchase.</p></>}
      {action === 'retry' && <details open><summary>Optional request revisions</summary><label>Revised request text<textarea value={requestText} onChange={event => setRequestText(event.target.value)} placeholder="Leave empty to keep the existing request"/></label>
        <label>Structured item specifications - JSON<textarea rows={5} value={specs} onChange={event => setSpecs(event.target.value)} placeholder='[{"id":"item-1","description":"Cotton tote bag","quantity":100,"parameters":{"material":"cotton"}}]'/></label>
        <label className="llm-option"><input type="checkbox" checked={changeProvider} onChange={event => setChangeProvider(event.target.checked)}/>Change supplier search settings or explicitly authorize a different provider budget</label>
        {changeProvider && <div className="form-row"><label>Supplier domains - comma separated<input value={domain} onChange={event => setDomain(event.target.value)} placeholder="Leave blank to search"/></label><label>Crustdata credit limit<input type="number" min="0" max="10" step="0.1" value={budget} onChange={event => setBudget(event.target.value)}/></label></div>}
        <p className="muted">A retry may call providers. Existing spending counts toward the same run limit; retrying does not reset budgets or payment identities.</p>
      </details>}
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary" disabled={busy}>{action === 'retry' ? 'Save review and retry' : action === 'manual_completion' ? 'Record manual handling' : 'Cancel remaining work'}</button>
    </form>
  </section>;
}
