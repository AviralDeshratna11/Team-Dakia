def worth_buying(quote, price_minor, pack) -> tuple[bool, str]:
    applicable = [q for q in quote.tiers if q <= quote.quantity]
    goods = quote.quantity * quote.tiers[max(applicable)] if applicable else 0
    useful = quote.stock is None and goods > 0 and price_minor * 10000 <= goods * pack.research_max_order_bps
    return useful, "Missing stock blocks eligibility; research price is within configured share of goods value." if useful else "Stock known or research price exceeds configured share of goods value."
