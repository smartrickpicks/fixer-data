"""Thought-cell extractor — the second-brain engine, done the way the reel describes.

Decomposes text into a graph, one cell at a time:
    nodes  = nouns  (person, team, place, concept)
    edges  = verbs  (the relationship / action between nodes)
    mods   = adverbs, each with a DECF score  ← the novel third axis (Zac's, to test)

The adverb+DECF layer is the short-term contextual CAG colour on top of the noun/verb
graph — "the how." Cells append to a bin (jsonl); the bin IS the layered memory stack.

Two callers, one engine: the Discord session-summary (one capture = one credit) and
Zac's own markdown memory stack. Pure stdlib + urllib. Reads OPENROUTER_API_KEY from env
(sourced from .env.constellation); never prints it.

    python3 extract.py --text "…"          # extract one cell, print + append to bin
    python3 extract.py --file notes.md     # same, from a markdown file
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, asdict, field

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("THOUGHTCELL_MODEL", "anthropic/claude-haiku-4.5")
ENV_PATH = "/Volumes/OttoVault/repos/airlock-config/secrets/.env.constellation"
BIN_PATH = os.environ.get("THOUGHTCELL_BIN", os.path.join(os.path.dirname(__file__), "memory-stack.jsonl"))

# DECF, C = Control (not Patience). Adverbs get scored on these four, 0-1 each.
DECF_KEYS = ("D", "E", "C", "F")


@dataclass(frozen=True)
class Extraction:
    nodes: list[dict]   # [{noun, kind}]           kind: person|team|place|concept|other
    edges: list[dict]   # [{from, verb, to}]
    mods: list[dict]    # [{adverb, target, decf:{D,E,C,F}}]


@dataclass(frozen=True)
class Cell:
    text: str
    extraction: Extraction
    ts: str
    sha: str = ""
    # source = where it came from + how fast it fades. half_life_h drives the decay.
    source: dict = field(default_factory=lambda: {"type": "general", "half_life_h": 12.0, "url": ""})

    def stamped(self) -> "Cell":
        body = {"text": self.text, "extraction": asdict(self.extraction), "ts": self.ts}
        sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        return Cell(self.text, self.extraction, self.ts, sha, self.source)


def _load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    # Fall back to the vault file without echoing the value.
    try:
        for line in open(ENV_PATH):
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    sys.exit("ERROR: OPENROUTER_API_KEY not set and not found in the vault.")


def _openrouter(messages: list[dict[str, str]], max_tokens: int = 700) -> str:
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "temperature": 0, "messages": messages}).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={"Authorization": f"Bearer {_load_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


_SYS = (
    "You break text into a knowledge graph for a sports-betting second brain. "
    "NODES are nouns (person, team, place, concept). EDGES are verbs linking two nouns "
    "(from -> verb -> to). MODS are adverbs (the 'how'); score each adverb on DECF, "
    "four floats 0-1: D=Dominance (force/assertiveness), E=Extraversion (outward/social "
    "energy), C=Control (precision/restraint), F=Formality (structure/rigor). "
    "Stay focused on betting-relevant meaning; drop filler. "
    "Return ONLY JSON: {\"nodes\":[{\"noun\":str,\"kind\":str}], "
    "\"edges\":[{\"from\":str,\"verb\":str,\"to\":str}], "
    "\"mods\":[{\"adverb\":str,\"target\":str,\"decf\":{\"D\":f,\"E\":f,\"C\":f,\"F\":f}}]}. "
    "No prose outside the JSON."
)


def extract(text: str) -> Extraction:
    raw = _openrouter([{"role": "system", "content": _SYS},
                       {"role": "user", "content": text}])
    start, end = raw.find("{"), raw.rfind("}")
    doc = json.loads(raw[start:end + 1]) if start >= 0 else {}
    return Extraction(
        nodes=doc.get("nodes", []),
        edges=doc.get("edges", []),
        mods=doc.get("mods", []),
    )


def append_to_bin(cell: Cell, path: str = BIN_PATH) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(asdict(cell)) + "\n")


# ── decay: the bin is short-term working memory, not an archive ──────────────
# Each cell fades on its own half-life (line moves in hours, injuries over days).
# weight = 2^(-age/half_life): 1.0 fresh, 0.5 at one half-life, → 0 as it ages out.
def weight(cell: dict, now: datetime.datetime | None = None) -> float:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        age_h = (now - datetime.datetime.fromisoformat(cell["ts"])).total_seconds() / 3600
    except (KeyError, ValueError):
        return 0.0
    hl = float(cell.get("source", {}).get("half_life_h") or 12.0)
    return 2 ** (-age_h / hl)


def live_cells(path: str = BIN_PATH, threshold: float = 0.1,
               now: datetime.datetime | None = None) -> list[tuple[float, dict]]:
    """Cells still above the decay threshold, freshest (highest weight) first."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    try:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            w = weight(c, now)
            if w >= threshold:
                out.append((w, c))
    except OSError:
        return []
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def sweep_bin(path: str = BIN_PATH, threshold: float = 0.1,
              now: datetime.datetime | None = None) -> int:
    """Rewrite the bin keeping only live cells — the bin 'cans' its own stale memory."""
    live = live_cells(path, threshold, now)
    with open(path, "w") as f:
        for _, c in live:
            f.write(json.dumps(c) + "\n")
    return len(live)


def _render(cell: Cell) -> str:
    e = cell.extraction
    lines = [f"cell {cell.sha}  ·  {len(e.nodes)} nodes / {len(e.edges)} edges / {len(e.mods)} mods"]
    lines.append("NODES (nouns): " + ", ".join(f"{n.get('noun')}·{n.get('kind')}" for n in e.nodes))
    lines.append("EDGES (verbs): " + "; ".join(
        f"{d.get('from')} →{d.get('verb')}→ {d.get('to')}" for d in e.edges))
    lines.append("MODS (adverbs · DECF):")
    for m in e.mods:
        d = m.get("decf", {})
        decf = " ".join(f"{k}{d.get(k, 0):.2f}" for k in DECF_KEYS)
        lines.append(f"  {m.get('adverb')} → {m.get('target')}   [{decf}]")
    return "\n".join(lines)


def _now_iso() -> str:
    # Stamp caller-side so the module has no hidden clock dependency in tests.
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main(argv: list[str]) -> None:
    text = ""
    if "--text" in argv:
        text = argv[argv.index("--text") + 1]
    elif "--file" in argv:
        text = open(argv[argv.index("--file") + 1]).read()
    if not text.strip():
        sys.exit("usage: extract.py --text \"…\"  |  --file notes.md")

    cell = Cell(text=text.strip(), extraction=extract(text), ts=_now_iso()).stamped()
    print(_render(cell))
    append_to_bin(cell)
    print(f"\n→ appended to {BIN_PATH}")


if __name__ == "__main__":
    main(sys.argv[1:])
