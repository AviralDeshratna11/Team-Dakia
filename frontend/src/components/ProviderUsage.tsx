import type { Run } from '../types';

export function ProviderUsage({ run }: { run: Run }) {
  if (run.request.mode !== 'live') return null;
  return <section className="research-entry">
    <div className="spread"><h3>Crustdata provider credits</h3><span className="pill">{String(run.request.provider_credit_limit)} credit cap</span></div>
    <p className="muted">Separate from INR research spending and Gemini token usage. Missing charge headers retain the reservation conservatively.</p>
    {run.provider_usage.length ? run.provider_usage.map(call => <div className="ledger-row" key={String(call.id)}>
      <div>{String(call.path)}<small>{String(call.state)} - HTTP {String(call.http_status ?? 'unknown')}</small></div>
      <strong>{String(call.charged ?? call.reserved)} credits</strong>
    </div>) : <p className="muted">No paid Crustdata calls recorded for this run.</p>}
  </section>;
}
