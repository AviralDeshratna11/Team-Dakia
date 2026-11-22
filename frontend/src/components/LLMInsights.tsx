import type { Run } from '../types';

export function LLMInsights({ run }: { run: Run }) {
  if (!run.request.llm_enabled) return null;
  const intent = run.request_interpretation;
  return <section className="card decision" aria-label="Gemini extraction results">
    <div className="spread"><h2>Gemini intelligence</h2><span className="pill">EXTRACTION ONLY</span></div>
    <p>Model-derived claims remain unverified. Eligibility, budgets, costs and approvals are controlled by code.</p>
    {run.state === 'WAITING_FOR_CLARIFICATION' && run.llm_usage.length >= 3 && <p className="muted">This run has used {run.llm_usage.length} of 4 model attempts. Supplying structured specifications avoids another parsing call.</p>}
    {intent ? <div className="research-entry"><h3>What we understood</h3><p>{String(intent.summary ?? '')}</p>
      <details><summary>Parsed requirements</summary><pre className="llm-json">{JSON.stringify(intent, null, 2)}</pre></details></div>
      : <p className="muted">Request interpretation will appear after parsing.</p>}
    {run.supplier_insights.map((insight, index) => {
      const supplierId = String(insight.supplier_id ?? '');
      const refs = Array.isArray(insight.evidence_ids) ? insight.evidence_ids.filter((id): id is string => typeof id === 'string') : [];
      const missing = Array.isArray(insight.missing_information) ? insight.missing_information.map(String) : [];
      return <div className="research-entry" key={`${supplierId}:${index}`}>
        <h3>{run.suppliers.find(s => s.id === supplierId)?.name ?? supplierId}</h3>
        <p>{String(insight.summary ?? '')}</p>
        {missing.length > 0 && <p className="muted">Still unknown: {missing.join('; ')}</p>}
        <details><summary>Extracted claims and sources</summary>
          <pre className="llm-json">{JSON.stringify(insight.facts ?? [], null, 2)}</pre>
          {refs.map(id => { const evidence = run.evidence.find(e => e.id === id); return evidence && <code key={id}>{evidence.provider}: {evidence.reference}</code>; })}
        </details>
      </div>;
    })}
    {run.extraction_warnings.length > 0 && <details><summary>Discarded or unsupported claims ({run.extraction_warnings.length})</summary>
      {run.extraction_warnings.map((warning, index) => <p className="muted" key={index}>{warning}</p>)}
    </details>}
    {run.llm_usage.length > 0 && <details><summary>Model call usage</summary>
      {run.llm_usage.map(call => <div key={String(call.id)}><p className="muted">{String(call.stage)} - {String(call.model)} - {String(call.state)} - {String(call.total_tokens ?? 'unknown')} total tokens</p>{call.diagnostic && <p className="muted">Validation: {String(call.diagnostic)}</p>}</div>)}
    </details>}
  </section>;
}
