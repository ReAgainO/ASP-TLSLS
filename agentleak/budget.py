"""Conservative per-request reservations; never pretend estimates are invoices."""
from decimal import Decimal

from .models import ModelCallError


class Budget:
    def __init__(self, max_cost, max_requests, previous_requests=0, previous_reserved="0"):
        self.max_cost = Decimal(str(max_cost))
        self.max_requests = max_requests
        self.requests = previous_requests
        self.reserved = Decimal(str(previous_reserved))

    def reserve(self, ceiling):
        amount = Decimal(str(ceiling))
        if amount <= 0 or not amount.is_finite():
            raise ValueError("Invalid request cost ceiling")
        if self.requests >= self.max_requests or self.reserved + amount > self.max_cost:
            raise ModelCallError("budget_exhausted")
        self.requests += 1
        self.reserved += amount
        # Reservations are never refunded, including timeouts, failed requests,
        # or missing usage. This is conservative exposure, not actual spending.
        return str(amount)
