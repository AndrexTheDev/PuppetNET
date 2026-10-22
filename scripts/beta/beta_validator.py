"""Beta S8: DocumentExtractor.validate robustness corpus (precision + recall).

Each case is one realistic model answer for one German/English news text.
expect=True  -> a correct, explicitly stated relation that must be ACCEPTED
expect=False -> must be REJECTED (hallucinated, hedged, denied, injected, ...)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO))


sys.path.insert(0, str(REPO / 'tests'))
from test_gemini_validator_corpus import CASES, _run  # noqa: E402


def main() -> int:
    fails = []
    recall_ok = recall_n = prec_ok = prec_n = 0
    for c in CASES:
        rels, rejected = _run(c)
        accepted = bool(rels)
        reason = next(iter(rejected), "-")
        ok = accepted == c["expect"]
        if c["expect"]:
            recall_n += 1
            recall_ok += ok
        else:
            prec_n += 1
            prec_ok += ok
        print(f"{'PASS' if ok else 'FAIL'} {c['cid']:<40} expect={'accept' if c['expect'] else 'reject'} got={'accept' if accepted else 'reject:' + reason}")
        if not ok:
            fails.append(c["cid"])
    print(f"\nrecall cases {recall_ok}/{recall_n}, precision cases {prec_ok}/{prec_n}")
    print(f"VALIDATOR BETA: {len(CASES) - len(fails)}/{len(CASES)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
