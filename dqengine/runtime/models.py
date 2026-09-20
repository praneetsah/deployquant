"""Slippage and fee models. The IR engine has neither (its parity model is
0 fees / 0 slippage); these exist because LEAN code sets them, and honoring a
ConstantSlippageModel keeps a pasted algorithm's fills faithful."""


class NullSlippageModel:
    def get_slippage_approximation(self, security, order) -> float:
        return 0.0


class ConstantSlippageModel:
    def __init__(self, slippage_percent: float = 0.0):
        self.slippage_percent = float(slippage_percent)

    def get_slippage_approximation(self, security, order) -> float:
        return security.price * self.slippage_percent


class NullFeeModel:
    def get_order_fee(self, *a, **k) -> float:
        return 0.0


class ConstantFeeModel:
    def __init__(self, fee: float = 0.0, *a, **k):
        self.fee = float(fee)

    def get_order_fee(self, *a, **k) -> float:
        return self.fee
