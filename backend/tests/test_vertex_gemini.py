import json
from types import SimpleNamespace
import pytest
from google.genai import types
from app.config import load_local_environment
from app.providers.gemini import GeminiClient
from app.providers.interfaces import IntegrationUnavailable
from app.providers.vertex_gemini import VertexGeminiTransport
from app.contracts.llm import ParsedIntent


@pytest.mark.parametrize("model",["claude-sonnet-4", "llama-3", "imagen-4", "veo-3", "publishers/anthropic/models/claude", "projects/other/models/gemini-2.5-flash"])
def test_non_gemini_and_resource_paths_rejected_before_auth(tmp_path,model):
    with pytest.raises(IntegrationUnavailable,match="GEMINI_ONLY"):
        GeminiClient(backend="vertex",model=model,journal_path=tmp_path/"unused.db")
    assert not (tmp_path/"unused.db").exists()


def test_vertex_sdk_uses_google_gemini_and_structured_output(tmp_path):
    calls=[]
    output={"summary":"Source specified module", "items":[], "clarification_question":"Which module?"}
    def generate(**kwargs):
        calls.append(kwargs)
        return types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="STOP",content=types.Content(parts=[types.Part(text=json.dumps(output))]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=20,candidates_token_count=30,total_token_count=50))
    sdk=SimpleNamespace(models=SimpleNamespace(generate_content=generate),close=lambda:None)
    client=GeminiClient(backend="vertex",model="gemini-2.5-flash",vertex_client=sdk,journal_path=tmp_path/"vertex.db")
    parsed=client.generate("run","parse_request","Extract only",{"request":"module"},ParsedIntent)
    assert parsed.clarification_question=="Which module?"
    assert len(calls)==1 and calls[0]["model"]=="gemini-2.5-flash"
    config=calls[0]["config"]
    assert config.response_mime_type=="application/json" and config.response_schema
    assert config.automatic_function_calling.disable and config.tools is None
    assert config.thinking_config.thinking_budget==0
    assert client.usage("run")[0]["total_tokens"]==50
    client.generate("run","parse_request","Extract only",{"request":"module"},ParsedIntent)
    assert len(calls)==1


def test_vertex_transport_rejects_other_operations_without_sdk_call():
    sdk=SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_:pytest.fail("must not call SDK")))
    transport=VertexGeminiTransport(sdk_client=sdk)
    for path in ["/v1beta/models/imagen-4:generateContent","/v1beta/models/gemini-2.5-flash:embedContent","/projects/example/secrets"]:
        with pytest.raises(IntegrationUnavailable,match="GEMINI_ONLY"):
            transport.post(path,{})


def test_vertex_schema_flattens_definitions_and_nullable_json_unions():
    from app.providers.vertex_gemini import vertex_schema
    from app.contracts.llm import ParsedIntent, SupplierExtraction
    for contract in [ParsedIntent,SupplierExtraction]:
        converted=vertex_schema(contract.model_json_schema())
        assert converted["type"]=="OBJECT"
        text=json.dumps(converted)
        assert '"$ref"' not in text and '"$defs"' not in text
        assert '"additionalProperties"' not in text and '"default"' not in text
        # SDK type validation is local and does not send a model request.
        types.Schema.model_validate(converted)


def test_local_environment_loader_preserves_existing_values_and_does_not_execute(tmp_path,monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL","gemini-2.5-flash-lite")
    monkeypatch.delenv("GEMINI_BACKEND",raising=False)
    monkeypatch.delenv("UNRELATED_SETTING",raising=False)
    path=tmp_path/".env"
    path.write_text('GEMINI_BACKEND=vertex\nGEMINI_MODEL=gemini-2.5-pro\nUNRELATED_SETTING=ignored\n',encoding="utf-8")
    load_local_environment(path)
    import os
    assert os.environ["GEMINI_BACKEND"]=="vertex"
    assert os.environ["GEMINI_MODEL"]=="gemini-2.5-flash-lite"
    assert "UNRELATED_SETTING" not in os.environ


def test_vertex_constructor_uses_credential_project_and_disables_retries(tmp_path,monkeypatch):
    from google import genai
    from google.oauth2 import service_account
    path=tmp_path/"service-account.json"
    path.write_text(json.dumps({"type":"service_account","project_id":"test-project"}),encoding="utf-8")
    marker=object()
    monkeypatch.setattr(service_account.Credentials,"from_service_account_info",lambda *a,**k:marker)
    observed={}
    def construct(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(close=lambda:None)
    monkeypatch.setattr(genai,"Client",construct)
    VertexGeminiTransport(credentials_file=str(path),project="test-project",location="global")
    assert observed["vertexai"] is True
    assert observed["project"]=="test-project" and observed["location"]=="global"
    assert observed["credentials"] is marker
    assert observed["http_options"].retry_options.attempts==1
