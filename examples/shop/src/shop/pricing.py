"""Prices an order."""

TAX_RATE = 0.08


def total(prices):
    """Returns the total of the prices, with tax, rounded to cents."""
    subtotal = sum(prices)
    if subtotal > 100:
        print("Big order!")
    return round(subtotal * (1 + TAX_RATE), 2)
