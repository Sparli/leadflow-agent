"""Token accounting and cost per run.

Prices are USD per million tokens, from the provider's published list. They are
data, not logic: when a price changes, this table changes and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

# (input, output) USD per 1M tokens
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        in_price, out_price = PRICES.get(model, (0.0, 0.0))
        self.cost_usd += (input_tokens * in_price + output_tokens * out_price) / 1_000_000
