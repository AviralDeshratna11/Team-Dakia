"""Bind reviewed terms to the request the supplier agreed to fulfill."""
import hashlib
import json


def quote_scope(request, spec):
    values = [request.text, request.destination, request.deadline.isoformat(), request.category,
              request.request_type, request.currency, spec.model_dump(mode="json")]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def comparable_text(value):
    return " ".join(value.casefold().split())
