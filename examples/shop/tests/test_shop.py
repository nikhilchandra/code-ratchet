from shop.cli import main
from shop.pricing import total
from shop.receipt import receipt


def test_total_adds_tax():
    assert total([10.00, 5.00]) == 16.20


def test_receipt_lists_items_and_the_total():
    assert receipt([("apple", 1.50), ("bread", 3.00)]) == "apple: 1.50\nbread: 3.00\nTotal: 4.86"


def test_the_command_line_prints_a_receipt(capsys):
    main(["apple=1.50", "bread=3.00"])
    assert capsys.readouterr().out == "apple: 1.50\nbread: 3.00\nTotal: 4.86\n"
