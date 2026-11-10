# shop: a tiny project to try code-ratchet on

shop prices an order and prints its receipt:

```bash
uv run python -c "from shop.cli import main; main(['apple=1.50', 'bread=3.00'])"
```

It is about 40 lines, with two design problems left in on purpose for ratchet to find:

- `receipt.py` works out the total itself, copying the tax rate, instead of asking `pricing.py`.
- `pricing.py` prints a message, although the rules say only the command line may print.

`.ratchet/` holds the two files you write for your own projects:

- [`.ratchet/rules.md`](.ratchet/rules.md): how the code should be organized, in plain words,
  with a block that lets ratchet check the rules without an agent.
- [`.ratchet/config.toml`](.ratchet/config.toml): how to run the tests, which files agents may
  change, and smaller caps than the defaults, since the project is tiny.

## Try it

ratchet works on a project that is its own git repository, so first set up a copy of this folder
outside the code-ratchet repository. From code-ratchet's root, give the script a folder that
does not exist yet:

```bash
uv run python examples/shop/try_it.py ~/shop                        # macOS or Linux
uv run python examples/shop/try_it.py C:\Users\you\Desktop\shop     # Windows
```

It copies the example there, makes the copy a git repository with one commit, and runs
`ratchet init`, which keeps the rules and settings above. It calls no agent. Then, in the copy:

```bash
ratchet audit -p "look at how the total is worked out"   # find the problems; you approve the list
ratchet problems                                         # read the list
ratchet refactor --max-jobs 1                            # fix one problem
```

The audit asks you any questions it has, then shows you its list of problems to approve. If the
`ratchet` command is not found, install it once with
`uv tool install git+https://github.com/nikhilchandra/code-ratchet`.

To set the copy up by hand instead: copy the folder, then in the copy run `git init`,
`git add -A`, `git commit -m "start"` and `ratchet init`. The commit leaves `.ratchet/` out,
because `.gitignore` lists it, and ratchet keeps those files in a git repository of their own.
