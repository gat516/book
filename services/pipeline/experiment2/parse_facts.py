"""Parse a pipe-format response (facts-v3 onward) into rows, keeping anything that doesn't fit visible.

Soft conformance means the model may drift from the format. A line that doesn't
parse is reported, never dropped silently and never guessed at.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATUSES = {"happened", "ongoing", "plan", "threat", "implied",  # facts-v3, plot-v4
            "opened", "advanced", "resolved",  # threads-v5
            "confirmed", "expected", "uncertain"}  # continuity-v6


def parse(text):
    # The list follows the last "## " heading; anything before it is the model's own thinking.
    headings = list(re.finditer(r"^## .*$", text, re.M))
    body = text[headings[-1].end():] if headings else text
    facts, unparsed = [], []
    for line in body.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip()  # tolerate stray bullets
        if not line:
            continue
        # The fact sentence is last, so a stray "|" inside it can't shift the other fields.
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) != 4 or not parts[3]:
            unparsed.append(line)
            continue
        category, status, names, fact = parts
        facts.append({"category": category.lower(), "status": status.lower(),
                      "names": [n.strip() for n in names.split(";") if n.strip()],
                      "fact": fact, "known_status": status.lower() in STATUSES})
    return facts, unparsed


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    facts, unparsed = parse((folder / "response.md").read_text())
    (folder / "facts.json").write_text(json.dumps({"facts": facts, "unparsed": unparsed},
                                                  indent=2, ensure_ascii=False))
    for f in facts:
        print(f"{f['category']:<12} {f['status']:<9} {', '.join(f['names'])}\n    {f['fact']}")
    print(f"\n{len(facts)} facts, {len(unparsed)} unparsed")
    for line in unparsed:
        print("  UNPARSED:", line)
