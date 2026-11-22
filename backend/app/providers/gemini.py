"""Gemini REST structured output; existing Python/httpx stack, no JS service needed.

Contracts: https://ai.google.dev/api/generate-content
JSON schema: https://ai.google.dev/gemini-api/docs/structured-output
"""
import hashlib
import json
import os
from pathlib import Path
import re
from typing import get_args, get_origin
import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine
from app.providers.interfaces import IntegrationUnavailable
from app.providers.vertex_gemini import VertexGeminiTransport, validate_gemini_model


class GeminiOutputError(IntegrationUnavailable):
    def __init__(self, code, diagnostic):
        self.code = code
        super().__init__(f"{code}: {diagnostic}; no extraction applied")


def normalize_extraction(schema, value):
    """Normalize representation differences only, never invent required facts."""
    if not isinstance(value,dict): return value
    value = dict(value)
    for name, field in schema.model_fields.items():
        if name not in value: continue
        annotation = field.annotation
        if get_origin(annotation) is list:
            if value[name] is None and field.default_factory is list:
                value[name] = []  # Optional lists mean no supplied information.
            item_type = get_args(annotation)[0]
            if isinstance(value[name],list) and isinstance(item_type,type) and issubclass(item_type,BaseModel):
                value[name] = [normalize_extraction(item_type,item) for item in value[name]]
        elif name == "value" and schema.__name__ in {"Parameter","ExtractedFact"} and type(value[name]) in {int,float}:
            value[name] = str(value[name])
    return value


class GeminiClient:
    def __init__(self, key=None, model=None, journal_path="gemini-calls.db", client=None, max_calls=4,
                 backend=None, credentials_file=None, project=None, location=None, vertex_client=None):
        self.backend = backend or os.getenv("GEMINI_BACKEND") or ("vertex" if credentials_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or vertex_client else "developer")
        if self.backend not in {"vertex", "developer"}:
            raise IntegrationUnavailable("GEMINI_ONLY: backend must be vertex or developer")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash" if self.backend == "vertex" else "gemini-3.1-flash-lite")
        validate_gemini_model(self.model)
        self.max_calls = max_calls
        if self.backend == "vertex":
            self.http = VertexGeminiTransport(credentials_file=credentials_file,project=project,location=location,sdk_client=vertex_client)
        else:
            key = key or os.getenv("GEMINI_API_KEY")
            if not key:
                raise IntegrationUnavailable("GEMINI_NOT_CONFIGURED: set GEMINI_API_KEY or use Vertex AI with GOOGLE_APPLICATION_CREDENTIALS")
            self.http = client or httpx.Client(base_url="https://generativelanguage.googleapis.com", headers={"x-goog-api-key":key}, timeout=45, follow_redirects=False)
        self.engine = create_engine("sqlite:///"+str(Path(journal_path).resolve()), connect_args={"timeout":30, "check_same_thread":False})
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS generations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, response TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER)")
            columns = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(generations)")}
            for column in ["diagnostic","finish_reason"]:
                if column not in columns: c.exec_driver_sql(f"ALTER TABLE generations ADD COLUMN {column} TEXT")

    def generate(self, run_id: str, stage: str, system: str, data: dict, schema: type[BaseModel]):
        content = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(content) > 60000:
            raise IntegrationUnavailable("GEMINI_INPUT_LIMIT: extraction input exceeds 60000 characters")
        contract = schema.model_json_schema()
        key = hashlib.sha256(json.dumps([self.backend,self.model,stage,system,content,contract],sort_keys=True).encode()).hexdigest()
        call_id = f"{run_id}:{key}"
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            prior = c.exec_driver_sql("SELECT state,response FROM generations WHERE id=?",(call_id,)).first()
            if prior:
                c.rollback()
                if prior[0] == "succeeded": return schema.model_validate_json(prior[1])
                raise IntegrationUnavailable("GEMINI_PRIOR_CALL_UNRESOLVED: no automatic regeneration after a failed or ambiguous call")
            count = c.exec_driver_sql("SELECT COUNT(*) FROM generations WHERE run_id=?",(run_id,)).scalar_one()
            if count >= self.max_calls:
                c.rollback()
                raise IntegrationUnavailable("GEMINI_CALL_LIMIT: maximum 4 generations per run, including clarification attempts")
            c.exec_driver_sql("INSERT INTO generations(id,run_id,stage,model,state) VALUES(?,?,?,?,'pending')",(call_id,run_id,stage,self.model))
            c.commit()
        payload = {"systemInstruction":{"parts":[{"text":system}]},
                   "contents":[{"role":"user","parts":[{"text":content}]}],
                   "generationConfig":{"responseMimeType":"application/json", "responseJsonSchema":contract, "maxOutputTokens":4096}}
        try:
            response = self.http.post(f"/v1beta/models/{self.model}:generateContent",json=payload)
        except httpx.TransportError:
            raise IntegrationUnavailable("GEMINI_NETWORK_ERROR: generation outcome unknown; no automatic paid retry") from None
        usage = {}
        result = None
        error = f"GEMINI_HTTP_{response.status_code}"
        diagnostic = None
        finish_reason = None
        if response.status_code == 200:
            try:
                body = response.json()
                usage = body.get("usageMetadata", {})
                candidates = body.get("candidates") or []
                if not candidates:
                    code = "GEMINI_OUTPUT_BLOCKED" if body.get("promptFeedback",{}).get("blockReason") else "GEMINI_SCHEMA_INVALID"
                    raise GeminiOutputError(code,"No usable output candidate returned")
                candidate = candidates[0]
                finish_reason = candidate.get("finishReason")
                if finish_reason != "STOP":
                    code = "GEMINI_OUTPUT_TRUNCATED" if finish_reason == "MAX_TOKENS" else "GEMINI_OUTPUT_BLOCKED"
                    raise GeminiOutputError(code,"Output did not complete; no automatic regeneration")
                parts = candidate.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought", False))
                result = schema.model_validate(normalize_extraction(schema,json.loads(text)))
            except GeminiOutputError as exc:
                error = exc.code
                diagnostic = str(exc).split(": ",1)[1].removesuffix("; no extraction applied")
            except ValidationError as exc:
                error = "GEMINI_SCHEMA_INVALID"
                # Locations/types are safe diagnostics; never include generated field values.
                diagnostic = "; ".join(".".join(map(str,e["loc"]))+" ("+e["type"]+")" for e in exc.errors(include_input=False,include_context=False,include_url=False)[:5])
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                error = "GEMINI_SCHEMA_INVALID"
                diagnostic = "Response was not valid structured JSON"
        def tokens(name):
            value = usage.get(name) if isinstance(usage,dict) else None
            return value if type(value) is int and value >= 0 else None
        with self.engine.begin() as c:
            c.exec_driver_sql("UPDATE generations SET state=?,response=?,input_tokens=?,output_tokens=?,total_tokens=?,diagnostic=?,finish_reason=? WHERE id=?",
                ("succeeded" if result is not None else "failed", result.model_dump_json() if result is not None else None,
                 tokens("promptTokenCount"),tokens("candidatesTokenCount"),tokens("totalTokenCount"),diagnostic,finish_reason,call_id))
        if result is None:
            if response.status_code == 200: raise GeminiOutputError(error,diagnostic or "Invalid structured output")
            raise IntegrationUnavailable(error+"; provider response body omitted")
        return result

    def usage(self, run_id):
        with self.engine.connect() as c:
            return [dict(r) for r in c.exec_driver_sql("SELECT id,stage,model,state,input_tokens,output_tokens,total_tokens,diagnostic,finish_reason FROM generations WHERE run_id=?",(run_id,)).mappings()]
