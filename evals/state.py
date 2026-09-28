#!/usr/bin/env python3
"""Print one value out of $FAKEHUB_STATE by dotted path, for a case's `case_check()`.

    python3 evals/state.py issues.100.state      -> OPEN | CLOSED
    python3 evals/state.py issues.100.labels      -> one label per line
    python3 evals/state.py prs.1.body             -> the PR body, verbatim
"""
import json
import os
import sys

d = json.load(open(os.environ["FAKEHUB_STATE"]))
node = d
for part in sys.argv[1].split("."):
    node = node[part]
if isinstance(node, list):
    for item in node:
        print(item)
elif node is None:
    pass
else:
    print(node)
