# Rules for this project

This file says what the code must become. Every agent reads it before it reads the code.

## 1. What the project is for

- shop prices an order and prints its receipt from the command line. It is a small example for
  trying code-ratchet.
- It will never keep anything between runs: no files, no database.

## 2. The parts

- `pricing`: the tax rate and the total of an order. It is the only place that knows the tax
  rate or how a total is worked out.
- `receipt`: the text of a receipt. It asks `pricing` for the total.
- `cli`: the command line. It reads the arguments and prints the receipt.
- `package`: the package's `__init__.py`, which holds only its docstring.
- `pricing` is the most likely to change, when the tax rate changes.

## 3. Who may use whom

- `pricing` uses nothing else in the package.
- `receipt` may use `pricing`.
- `cli` may use `receipt` and `pricing`.

## 4. The outside world

- Only `cli` may read input or print. `pricing` and `receipt` must be testable with no setup.

## 5. Deliberate exceptions and code to leave alone

- None.

## Machine-readable rules

```toml
[parts]
package = "shop"
pricing = "shop.pricing"
receipt = "shop.receipt"
cli = "shop.cli"

[may_use]
package = []
pricing = []
receipt = ["pricing"]
cli = ["receipt", "pricing"]

[may_touch_outside_world]
parts = ["cli"]

[skip]
modules = []
```

## Decided in audits

Rules that audits settled with the owner are added below.
