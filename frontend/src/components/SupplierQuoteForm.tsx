import { useState } from 'react';
import { api } from '../api/client';
import type { Run } from '../types';
import type { components } from '../types/schema';

type Submission = components['schemas']['SupplierQuoteSubmission'];

export function SupplierQuoteForm({ run, task, reviewer, busy, onSaved }: {
  run: Run;
  task: Run['human_reviews'][number];
  reviewer: string;
  busy: boolean;
  onSaved: (updated: Run) => void;
}) {
  const items = run.specs.filter(spec => !spec.unresolved_questions.length &&
    (!task.spec_ids.length || task.spec_ids.includes(spec.id)) &&
    (run.request.request_type !== 'purchase' || spec.quantity !== null));
  const [itemId, setItemId] = useState(items[0]?.id ?? '');
  const [supplierId, setSupplierId] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const item = items.find(spec => spec.id === itemId);
  const supplier = run.suppliers.find(candidate => candidate.id === supplierId);
  if (!item || run.orders.some(order => ['PAY_PENDING', 'RECONCILING'].includes(order.state))) return null;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!item) return;
    const form = new FormData(event.currentTarget);
    const value = (name: string) => String(form.get(name) ?? '').trim();
    setError('');
    setSaved(false);
    setSaving(true);
    try {
      const submission: Submission = {
        spec_id: item.id, supplier_name: supplier?.name ?? value('supplierName'),
        supplier_domain: supplier?.domain ?? value('supplierDomain'),
        supplier_kind: value('supplierKind') as Submission['supplier_kind'],
        identity_reference: value('identity'), quote_reference: value('quoteReference'), quote_excerpt: value('excerpt'),
        reviewer: reviewer.trim() || value('quoteReviewer'),
        quantity: Number(value('quantity')), unit: item.unit, destination: value('destination'),
        delivery_at: new Date(value('delivery')).toISOString(), expires_at: new Date(value('expiry')).toISOString(),
        total_minor: Math.round(Number(value('total')) * 100),
        includes_all_costs: true, requirements_and_availability_confirmed: true,
        authorized_distributor: form.has('authorized'), manufacturer_verified: form.has('manufacturer'),
      };
      onSaved(await api<Run>(`/runs/${run.id}/human-reviews/${task.id}/supplier-quotes`, 'POST', submission));
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    } finally { setSaving(false); }
  }

  return <details>
    <summary>Add supplier quote</summary>
    <p>Use a quote issued by the supplier. Record its terms and the source you used to verify the supplier’s identity.</p>
    <form onSubmit={submit}>
      <div className="form-row">
        <label>Requested item<select value={itemId} onChange={event => setItemId(event.target.value)}>{items.map(spec => <option value={spec.id} key={spec.id}>{spec.description}</option>)}</select></label>
        <label>Supplier<select value={supplierId} onChange={event => setSupplierId(event.target.value)}><option value="">Enter a supplier</option>{run.suppliers.map(candidate => <option value={candidate.id} key={candidate.id}>{candidate.name} ({candidate.domain})</option>)}</select></label>
      </div>
      {!supplier && <div className="form-row"><label>Supplier name<input name="supplierName" required minLength={2}/></label><label>Supplier domain<input name="supplierDomain" required placeholder="supplier.example"/></label></div>}
      <div className="form-row">
        <label>Supplier type<select name="supplierKind" required key={supplierId} defaultValue={supplier?.kind === 'unknown' ? '' : supplier?.kind ?? ''}><option value="">Choose supplier type</option><option value="manufacturer">Manufacturer</option><option value="distributor">Distributor</option><option value="retailer">Retailer</option><option value="service_provider">Service provider</option><option value="trader">Trader</option></select></label>
        <label>Supplier identity source<input name="identity" required minLength={5} placeholder="Official company listing or identity reference"/></label>
      </div>
      <label>Supplier quote reference<input name="quoteReference" required minLength={3} placeholder="Quote number, email reference or official URL"/></label>
      <label>Quote terms<textarea name="excerpt" required minLength={10} maxLength={8000} placeholder="Paste the supplier’s item, availability, price and delivery terms."/></label>
      <div className="form-row">
        <label>Committed quantity ({item.unit})<input name="quantity" type="number" required min={1} step={1} key={item.id} defaultValue={item.quantity ?? ''}/></label>
        <label>Total delivered price in INR<input name="total" type="number" required min="0.01" max="1000000000" step="0.01"/></label>
      </div>
      <label>Quoted destination<input name="destination" required defaultValue={run.request.destination}/></label>
      <div className="form-row"><label>Delivery by (local time)<input name="delivery" required type="datetime-local"/></label><label>Quote expires (local time)<input name="expiry" required type="datetime-local"/></label></div>
      {!reviewer.trim() && <label>Quote reviewed by<input name="quoteReviewer" required minLength={2}/></label>}
      <label className="llm-option"><input type="checkbox" required/>I checked supplier identity, specification match, available quantity, and delivery against the source.</label>
      <label className="llm-option"><input type="checkbox" required/>The total includes all applicable delivery charges, duties, taxes, and fees.</label>
      {run.request.category === 'electronics' && <label className="llm-option"><input type="checkbox" name="authorized"/>Verified manufacturer authorization</label>}
      {run.request.category === 'leather_goods' && <label className="llm-option"><input type="checkbox" name="manufacturer"/>Verified manufacturer activity</label>}
      {error && <p role="alert">{error}</p>}
      {saved && <p role="status">Quote saved. Choose Evaluate supplier quotes to check eligibility.</p>}
      <button className="secondary" disabled={saving || busy}>{saving ? 'Saving quote…' : 'Save supplier quote'}</button>
    </form>
  </details>;
}
