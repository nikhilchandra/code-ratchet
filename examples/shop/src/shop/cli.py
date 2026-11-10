"""The command line: `shop apple=1.50 bread=3.00` prints a receipt."""

import sys

from shop.receipt import receipt


def main(argv=None):
    """Reads name=price pairs from the command line and prints their receipt."""
    args = sys.argv[1:] if argv is None else argv
    items = [(arg.split("=")[0], float(arg.split("=")[1])) for arg in args]
    print(receipt(items))
