"""Create the deterministic, disposable repository used by routing evaluations."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


FILES = {
    "shop/__init__.py": '"""Small shopping cart package."""\n',
    "shop/discount.py": '''"""Discount calculation."""

def discount(total: float, rate: float) -> float:
    """Return the amount due after a percentage discount."""
    return total * (1 - rate)

def valid_rate(rate: float) -> bool:
    return 0 <= rate <= 1
''',
    "shop/pricing.py": '''"""Prices and totals."""
from .discount import discount

def subtotal(prices: list[float]) -> float:
    return sum(prices)

def total(prices: list[float], rate: float = 0) -> float:
    before = subtotal(prices)
    after = discount(before, rate)
    return discount(after, rate)

def round_money(amount: float) -> float:
    return round(amount, 2)
''',
    "shop/cart.py": '''"""A cart backed by the pricing module."""
from .pricing import total

class Cart:
    def __init__(self) -> None:
        self.prices: list[float] = []

    def add(self, price: float) -> None:
        self.prices.append(price)

    def checkout(self, rate: float = 0) -> float:
        return total(self.prices, rate)
''',
    "tests/test_shop.py": '''from shop.discount import discount
from shop.pricing import subtotal
from shop.cart import Cart

def test_discount():
    assert discount(100, .1) == 90

def test_subtotal():
    assert subtotal([2, 3]) == 5

def test_cart_add():
    cart = Cart()
    cart.add(4)
    assert cart.prices == [4]
''',
    ".hermes.md": "Hermes-Control: verify Python tasks with `python -m pytest -q`.\n",
}


def build(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    if destination == Path.home() / ".claude" or Path.home() / ".claude" in destination.parents:
        raise ValueError("evaluation project must not be under ~/.claude")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in FILES.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-24T00:00:00+00:00",
               "GIT_COMMITTER_DATE": "2026-09-24T00:00:00+00:00"}
        subprocess.run(["git", *args], cwd=destination, check=True,
                       capture_output=True, text=True, env=env)

    git("init", "-b", "main")
    git("config", "user.name", "Routing Eval")
    git("config", "user.email", "routing-eval@example.invalid")
    git("add", ".")
    git("commit", "-m", "Initial shop fixture")
    git("switch", "-c", "feature/rounding")
    pricing = destination / "shop/pricing.py"
    pricing.write_text(pricing.read_text(encoding="utf-8").replace(
        "return round(amount, 2)", "return round(amount + 0.005, 2)"), encoding="utf-8")
    git("add", "shop/pricing.py")
    git("commit", "-m", "Adjust rounding at half cents")
    git("switch", "main")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()
    print(build(args.destination or Path(tempfile.mkdtemp(prefix="hermes-routing-eval-"))))


if __name__ == "__main__":
    main()
