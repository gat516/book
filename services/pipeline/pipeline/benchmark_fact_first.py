"""Compatibility entry point for the pinned fact-first benchmark.

The implementation lives in :mod:`pipeline.fact_first` so production adapters and
the historical benchmark execute the same parser, validators, and prompts.  Keeping
this import path preserves existing replay artifacts and command lines.
"""

from . import fact_first as _core

for _name, _value in vars(_core).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals()[_name] = _value

del _name, _value, _core

if __name__ == "__main__":
    main()
