from datetime import timedelta
import json
from pathlib import Path
from app.contracts import Supplier, Quote, Evidence, now
from app.config import pack_for


def supported_spec(spec, category):
    if category == "leather_goods":
        return spec.parameters.get("location") == "Uttar Pradesh"
    from app.services.specification_parser import ITEMS
    item = spec.parameters.get("fixture_item")
    if item not in ITEMS:
        return False
    required = ITEMS[item][2]
    return all(spec.parameters.get(k) == value for k, value in required.items()) and not spec.substitutes


class FixtureDiscovery:
    """Synthetic companies and quotes; fixture verification is not real-world verification."""
    def discover(self, request, specs):
        pack = pack_for(request.category)
        rows = json.loads(Path(__file__).with_name("catalog.json").read_text()).get(request.category,[])
        if not rows:
            return [], [], [Evidence(reference="fixture://catalog/unsupported",provider="fixture_catalog",fact="demo coverage",status="unverified",
                value="No synthetic supplier catalogue exists for this request. Use live data or a human reviewer; no suppliers have been invented.")]
        canonical, suppliers, quotes, evidence = {}, [], [], []
        for row in rows:
            key = row["canonical_id"] or row["id"]
            if key in canonical:
                canonical[key].aliases.append(row["name"])
                continue
            supplier = Supplier.model_validate({k: v for k, v in row.items() if k not in {"multiplier", "signals"}})
            canonical[key] = supplier
            suppliers.append(supplier)
            for spec in specs:
                quantity = spec.quantity or 1  # 1 is only a non-purchase shortlist placeholder.
                qid = f"{spec.id}:{supplier.id}"
                observed = now()
                fact = Evidence(reference=f"fixture://catalog/{request.category}/{supplier.id}", provider="fixture_catalog",
                                observed_at=observed, fact="identity, trust signals, stock, shipping, specifications and lead time",
                                value="Synthetic scenario: " + json.dumps(row["signals"]))
                evidence.append(fact)
                missing = request.scenario == "missing_stock" and supplier.id == "bright"
                timeout = request.scenario == "provider_timeout"
                price = {"esp32": 28000, "ldo": 1200, "capacitor": 100, "usb": 2500}.get(spec.parameters.get("fixture_item"), 20000)
                quotes.append(Quote(id=qid, supplier_id=supplier.id, spec_id=spec.id,
                    tiers={1: price * row["multiplier"] // 100, 500: price * row["multiplier"] * 9 // 1000},
                    quantity=quantity, stock=None if missing or timeout else max(10000, quantity),
                    shipping_minor=10000, lead_days=None if timeout else 5,
                    delivery_at=None if timeout else observed + timedelta(days=5),
                    serves_destination=True if "india" in request.destination.lower() or "bengaluru" in request.destination.lower() else None,
                    matches_spec=supported_spec(spec, request.category), observed_at=observed, expires_at=observed + timedelta(hours=pack.freshness_hours),
                    evidence_ids=[fact.id], status="unverified" if timeout else "verified", signals=row["signals"]))
        return suppliers, quotes, evidence
