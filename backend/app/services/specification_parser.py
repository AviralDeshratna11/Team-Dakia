"""Bounded demo parser. Arbitrary prose requires the future structured LLM adapter."""
import re
from app.contracts import Spec

ITEMS = {
    "esp32": (r"esp32", "ESP32-S3 module", {"part_number": "ESP32-S3"}),
    "ldo": (r"ldo|regulator", "3.3V LDO, >=500mA, SOT-23", {"voltage": "3.3V", "current": ">=500mA", "package": "SOT-23"}),
    "capacitor": (r"capacitor", "10uF 0805 capacitor", {"capacitance": "10uF", "package": "0805"}),
    "usb": (r"usb", "USB-C connector", {"connector": "USB-C"}),
}


class FixtureParser:
    def parse(self, request):
        if request.specs:
            if any(s.unresolved_questions for s in request.specs):
                return request.specs, next(q for s in request.specs for q in s.unresolved_questions)
            if request.request_type == "purchase" and any(s.quantity is None for s in request.specs):
                return request.specs, "Provide a positive quantity for each item in the structured specification editor."
            return request.specs, None
        text = request.text.lower()
        if request.category == "leather_goods":
            if "uttar pradesh" not in text:
                return [], "This fixture supports Uttar Pradesh manufacturers. Confirm that location or provide a live adapter."
            return [Spec(id="leather-shortlist", description="Leather-goods manufacturer shortlist", parameters={"location": "Uttar Pradesh"})], None
        matched = [(key, desc, params) for key, (pattern, desc, params) in ITEMS.items() if re.search(pattern, text)]
        if not matched:
            return [], "Provide structured item specifications; the demo parser only recognizes ESP32-S3, the example LDO, 10uF 0805 capacitors and USB-C."
        if "ldo" in [m[0] for m in matched] and not all(re.search(p, text) for p in [r"3\.3\s*v", r"500\s*ma", r"sot[- ]?23"]):
            return [], "What output voltage, minimum current, package, and quantity must the regulator meet?"
        # Only an explicit per-item count is accepted for a multi-item BOM.
        quantity = re.search(r"(\d+)\s+(?:of\s+)?each\b", text)
        if not quantity and len(matched) == 1:
            quantity = re.search(r"\b(\d+)\s+(?:pieces?|units?)\b", text)
        if quantity is None or int(quantity[1]) <= 0:
            return [], "How many of each item do you need? A build count alone does not specify per-line quantities. Answer e.g. '500 of each', or submit structured specs."
        specs = [Spec(id=key, description=desc, part_number=params.get("part_number"), parameters=dict(params, fixture_item=key), quantity=int(quantity[1])) for key, desc, params in matched]
        # The fixture catalogue models only these reference specifications.
        if "esp32" in [m[0] for m in matched] and "esp32-s3" not in text:
            return [], "Confirm ESP32-S3 or provide exact structured specifications."
        if "capacitor" in [m[0] for m in matched] and not ("0805" in text and re.search(r"10\s*(?:u|µ|μ)f", text)):
            return [], "Confirm 10uF and 0805, or provide exact structured specifications."
        if "usb" in [m[0] for m in matched] and not re.search(r"usb[- ]?c", text):
            return [], "Confirm USB-C or provide exact structured specifications."
        return specs, None
