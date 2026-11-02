from decimal import Decimal, ROUND_HALF_UP
def discounted(amount, percent):
    percent = Decimal(str(percent))
    if not 0 <= percent <= 100:
        raise ValueError("percent outside 0..100")
    return amount * (1 - percent / 100)
