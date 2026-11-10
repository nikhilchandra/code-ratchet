"""Writes the text of a receipt."""


def receipt(items):
    """Returns a receipt for a list of (name, price) pairs, one line each and then the total."""
    lines = [f"{name}: {price:.2f}" for name, price in items]
    subtotal = sum(price for _, price in items)
    lines.append(f"Total: {round(subtotal * 1.08, 2):.2f}")
    return "\n".join(lines)
