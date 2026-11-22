import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { components } from '../types/schema';

type Balance = components['schemas']['WalletBalance'];
const format = (amount: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(amount / 100);

export function SyntheticWallet({ revision }: { revision: string }) {
  const [balance, setBalance] = useState<Balance>();
  const [error, setError] = useState('');
  const [amount, setAmount] = useState('10000');
  const [pending, setPending] = useState(false);
  const [funding, setFunding] = useState<{ amount_minor: number; idempotency_key: string }>();

  useEffect(() => {
    let current = true;
    api<Balance>('/payments/wallet').then(value => { if (current) setBalance(value); })
      .catch(err => { if (current) setError((err as Error).message); });
    return () => { current = false; };
  }, [revision]);

  async function fund(event: React.FormEvent) {
    event.preventDefault();
    const request = funding ?? { amount_minor: Math.round(Number(amount) * 100), idempotency_key: crypto.randomUUID() };
    setFunding(request);
    setPending(true);
    setError('');
    try {
      setBalance(await api<Balance>('/payments/wallet/fund', 'POST', request));
      setFunding(undefined);
    } catch (err) {
      setError((err as Error).message);
    } finally { setPending(false); }
  }

  return <section className="card decision" aria-label="Synthetic payment wallet">
    <h2>Synthetic payment wallet</h2>
    {balance && <p>{format(balance.available_minor)} available · {format(balance.spent_minor)} spent</p>}
    <form className="budget-form" onSubmit={fund}>
      <label>Test funds in INR<input type="number" required min="0.01" max="1000000" step="0.01" value={amount} disabled={Boolean(funding)} onChange={event => setAmount(event.target.value)}/></label>
      <button className="secondary" disabled={pending}>{funding ? 'Retry top-up' : 'Add test funds'}</button>
    </form>
    {error && <p role="alert">{error}</p>}
  </section>;
}

export function PayoutPreview({ runId, orderId }: { runId: string; orderId: string }) {
  const [preview, setPreview] = useState('');
  const [error, setError] = useState('');
  return <div>
    <button className="secondary" onClick={async () => {
      setError('');
      try {
        const result = await api(`/runs/${runId}/orders/${encodeURIComponent(orderId)}/payout-preview`);
        setPreview(JSON.stringify(result, null, 2));
      } catch (err) { setError((err as Error).message); }
    }}>Preview RazorpayX UPI payout</button>
    {preview && <pre className="llm-json">{preview}</pre>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
