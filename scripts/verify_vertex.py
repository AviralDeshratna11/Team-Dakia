"""Two bounded Vertex Gemini extraction calls. No Crustdata or other model/service calls.

Uses synthetic supplier evidence for stage two, so this verifies LLM setup rather
than live supplier verification. Successful outputs are cached; no automatic retry.
"""
from datetime import timedelta
import json
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/"backend"))
from app.config import load_local_environment
from app.contracts import WorkflowRun, SourcingRequest, now
from app.providers.gemini import GeminiClient
from app.providers.fixtures.discovery import FixtureDiscovery
from app.services.llm_extraction import LLMExtractionService


def main():
    load_local_environment()
    folder=root/"tmp"/"vertex-verify"
    folder.mkdir(parents=True,exist_ok=True)
    client=GeminiClient(backend="vertex",journal_path=folder/"generations.db",max_calls=2)
    service=LLMExtractionService(client)
    deadline=now()+timedelta(days=21)
    run=WorkflowRun(request=SourcingRequest(text=f"Source 500 ESP32-S3-WROOM-1-N8R8 modules. No substitutions. Deliver to Bengaluru, India by {deadline.date().isoformat()}.",
        category="electronics",destination="Bengaluru, India",deadline=deadline,mode="fixture",llm_enabled=True))
    try:
        print("Checking stage 1: request extraction using Vertex Google Gemini only.",flush=True)
        run.specs,question=service.parse(run)
        if question: raise RuntimeError("Request needs clarification: "+question)
        assert run.specs and run.specs[0].quantity==500
        run.suppliers,run.quotes,run.evidence=FixtureDiscovery().discover(run.request,run.specs)
        print("Checking stage 2: supplier evidence extraction (synthetic evidence, no Crustdata calls).",flush=True)
        service.extract(run)
        usage=client.usage(run.id)
        assert len(usage)==2 and all(c["state"]=="succeeded" for c in usage)
        assert run.supplier_insights
        report={"backend":"vertex","model":client.model,"location":"global","run_id":run.id,
            "model_calls":len(usage),"usage":usage,"stage_1":"passed","stage_2":"passed",
            "evidence_mode":"synthetic fixtures","crustdata_calls":0,"other_model_calls":0,
            "interpretation":run.request_interpretation,"insights":run.supplier_insights,"warnings":run.extraction_warnings}
        target=folder/"result.json"
        target.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({k:v for k,v in report.items() if k not in {"interpretation","insights","warnings"}},indent=2),flush=True)
        print("PASS: both Vertex Gemini stages. Local report:",target,flush=True)
    finally:
        client.http.close()
        client.engine.dispose()


if __name__=="__main__": main()
