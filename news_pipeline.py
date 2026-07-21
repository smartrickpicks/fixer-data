"""RSS → mo gate → tagged, tiered news → Discord-ready feed.  The Fixer's ear.

Upgrades rss.py's crude substring filter into the real funnel we designed:

    collect (RSS)  →  mo PPMI gate (deterministic, $0)  →  tag  →  tier  →  emit

Only items the mo gate says are NEAR tonight's slate survive; each survivor gets
relevance/factor/sentiment tags derived from WHICH gates it hit; COLD items are
counted as noise and dropped (never extracted, never posted). Freshest-relevant
floats to the top. The expensive LLM extraction (extract.py) only fires on HOT/
WARM items, and only when --extract is passed — the gate itself costs nothing.

    python3 news_pipeline.py                 # dry run: collect→gate→tag→emit (no LLM)
    python3 news_pipeline.py --extract       # also extract HOT/WARM into the bin (LLM)
    python3 news_pipeline.py --out /path/news_data.json   # write feed somewhere else

The mo gate here is a faithful pure-Python port of mo-sense coldRead
(topology.ts / coldread.ts): trigger = fraction of tokens mapped, topDoc = which
game the item circles, bridges = distinctive tokens that pin a specific game (the
"news is touching the slate" signal). Canonical impl: ~/Downloads/mo-sense. Keep
in sync; set MO_SENSE_URL later to call the real sidecar instead of this port.

Pure stdlib + urllib. No key needed for the gate; OPENROUTER_API_KEY (vault) only
for --extract.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

from extract import Cell, extract, append_to_bin, sweep_bin, _now_iso

# ── sources ──────────────────────────────────────────────────────────────────
FIXER_FEED = "https://raw.githubusercontent.com/smartrickpicks/fixer-data/main/picks_data.json"
FEEDS = [
    ("https://www.espn.com/espn/rss/mlb/news", "espn-mlb"),
    ("https://www.cbssports.com/rss/headlines/mlb/", "cbs-mlb"),
]
OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_data.json")

# ── tiering thresholds (tune here) ───────────────────────────────────────────
HOT_SCORE = 0.55     # weighted-hook mass + a game pinned → clearly about a slate game
WARM_SCORE = 0.20    # touches the slate but doesn't pin a game
HALF_LIFE_H = {"injury": 72.0, "lineup": 24.0, "line": 6.0,
               "weather": 12.0, "suspension": 96.0, "roster": 48.0, "general": 12.0}

STOP = {"new", "los", "san", "the", "york", "city", "bay", "white", "red", "blue",
        "game", "team", "says", "will", "with", "from", "over", "into", "after",
        "night", "this", "that", "back", "star", "mlb", "baseball", "league"}

# Sportsbook ads dressed as headlines — drop them, they're not news.
PROMO_RX = re.compile(
    r"promo code|bonus code|bonus bets|\$\d+\s*(?:in\s*)?bonus|odds boost|sign[\-\s]?up offer|"
    r"use code|first bet|no[\-\s]?sweat|bet \$\d+ get|welcome offer|betmgm|draftkings promo|fanduel promo",
    re.I)

# ── deterministic tag lexicons (attributed, never asserted) ──────────────────
FACTOR_RX = {
    "injury": r"injur|\bout\b|\bil\b|\bdl\b|scratch|strain|sore|hamstring|elbow|"
              r"shoulder|questionable|day-to-day|placed on|left the game|mri|surgery",
    "lineup": r"lineup|starting|starter|probable|rotation|scratched from|will start|"
              r"gets the (?:ball|nod)|batting (?:first|second|third|cleanup)",
    "line":   r"\bodds\b|\bline\b|spread|moneyline|line move|sharp|favorite|underdog|"
              r"betting|over/under|total|\bo/u\b",
    "weather": r"weather|rain|wind|postpone|delay|forecast|tarp|humidity",
    "suspension": r"suspend|suspension|ejected|banned|appeal|discipline",
    "roster": r"traded|acquire|designated|call(?:ed)? up|option(?:ed)?|recall|"
              r"activated|reinstate|sign(?:ed|s)\b|waiver",
}
NEG_CUES = ["out", "injured", "injury", "doubtful", "questionable", "scratched",
            "strained", "sore", "placed on il", "suspended", "ejected", "benched",
            "slump", "struggling", "demoted", "optioned", "surgery", "postponed"]
POS_CUES = ["returns", "activated", "cleared", "reinstated", "healthy",
            "back in the lineup", "recalled", "called up", "dominant", "cruising",
            "hot streak", "on fire"]


def get_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(f"{url}?t={_cachebust()}" if "?" not in url else url,
                                timeout=timeout) as r:
        return json.loads(r.read())


def _cachebust() -> str:
    # avoid Date.now() dependence for testability; a per-run nonce from the feed len
    return "1"


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) >= 4 and t not in STOP]


# ── slate topology: each game a "doc"; distinctive tokens weigh more (PPMI floor) ──
def build_slate(feed: dict):
    """Return (games, vocab_weight, tok_to_games, team_of).

    games       : [{key, away, home, tokens:set}]
    vocab_weight: token -> distinctiveness weight  (idf over games; the PPMI-floor
                  stand-in — a token in one game is a strong pin, a common word weak)
    tok_to_games: token -> set(gameKey)
    team_of     : token -> team label (best guess, for sentiment attribution)
    """
    games = []
    for g in feed.get("games", []):
        away, home = g.get("away") or "", g.get("home") or ""
        toks = set()
        team_tokens = {}
        for label, key in ((away, "away"), (home, "home")):
            for t in tokens(label):
                toks.add(t); team_tokens[t] = label
        for key in ("hp", "ap"):                      # starting pitchers
            for t in tokens(g.get(key) or ""):
                toks.add(t); team_tokens.setdefault(t, home if key == "hp" else away)
        if not toks:
            continue
        gk = f"{away}@{home}".strip("@") or f"game{len(games)}"
        games.append({"key": gk, "away": away, "home": home,
                      "tokens": toks, "team_tokens": team_tokens})

    n = max(1, len(games))
    df = {}
    tok_to_games, team_of = {}, {}
    for gm in games:
        for t in gm["tokens"]:
            df[t] = df.get(t, 0) + 1
            tok_to_games.setdefault(t, set()).add(gm["key"])
            team_of.setdefault(t, gm["team_tokens"].get(t, ""))
    # idf-style distinctiveness: rare-across-games ⇒ high weight (pins a game).
    vocab_weight = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
    return games, vocab_weight, tok_to_games, team_of


# ── the mo gate: faithful pure-Python port of coldRead(topo, utterance) ──────
def cold_read(text: str, vocab_weight, tok_to_games):
    toks = tokens(text)
    if not toks:
        return {"trigger": 0.0, "score": 0.0, "topDoc": None, "bridges": [], "hooks": []}
    hooks = [t for t in toks if t in vocab_weight]
    unfamiliar = [t for t in toks if t not in vocab_weight]
    trigger = len(hooks) / max(1, len(hooks) + len(unfamiliar))

    doc_mix, bridges = {}, []
    seen = set()
    for t in hooks:
        w = vocab_weight[t]
        gs = tok_to_games.get(t, set())
        if gs:
            share = w / len(gs)
            for gk in gs:
                doc_mix[gk] = doc_mix.get(gk, 0.0) + share
        if t not in seen and len(gs) == 1:            # pins exactly one game = a bridge
            bridges.append(t); seen.add(t)
    top_doc = max(doc_mix, key=doc_mix.get) if doc_mix else None
    # relevance score: mass on the single game it most circles, dampened by spread.
    score = 0.0
    if top_doc:
        top = doc_mix[top_doc]
        total = sum(doc_mix.values()) or 1.0
        score = (top / total) * min(1.0, top / 1.5) * (0.5 + 0.5 * trigger)
    return {"trigger": round(trigger, 3), "score": round(score, 3),
            "topDoc": top_doc, "bridges": bridges[:6], "hooks": hooks[:12]}


def factors(text: str) -> list[str]:
    t = text.lower()
    hits = [f for f, rx in FACTOR_RX.items() if re.search(rx, t)]
    return hits or ["general"]


def _cue_hit(cues, t):
    # word-boundary match so "out" doesn't fire inside "standout"/"about".
    for c in cues:
        if re.search(rf"\b{re.escape(c)}\b", t):
            return c
    return None


def sentiment(text: str, team_of, hooks) -> dict:
    t = text.lower()
    neg = _cue_hit(NEG_CUES, t)
    pos = _cue_hit(POS_CUES, t)
    # naive negation guard: "not cleared" / "won't return" reads as negative, not positive.
    if pos and re.search(rf"\b(?:not|no|won'?t|isn'?t|never)\b[\w\s]{{0,12}}{re.escape(pos)}", t):
        pos = None
        neg = neg or "not " + "cleared"
    tag = "negative" if neg and not pos else "positive" if pos and not neg else "neutral"
    cue = neg or pos or ""
    team = next((team_of.get(h, "") for h in hooks if team_of.get(h)), "")
    return {"tag": tag, "cue": cue, "team": team}   # heuristic hint, not an assertion


def half_life(factor_tags: list[str]) -> float:
    return max(HALF_LIFE_H.get(f, 12.0) for f in factor_tags)


def fetch_items(url: str):
    with urllib.request.urlopen(url, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip()
        if title:
            out.append((title, desc, link))
    return out


def run(do_extract: bool, out_path: str) -> dict:
    feed = get_json(FIXER_FEED)
    slate_date = feed.get("date", "")
    games, vocab_weight, tok_to_games, team_of = build_slate(feed)
    print(f"slate {slate_date}: {len(games)} games · {len(vocab_weight)} distinctive tokens",
          file=sys.stderr)

    kept, dropped, seen_sha = [], 0, set()
    for url, name in FEEDS:
        try:
            items = fetch_items(url)
        except Exception as e:                       # a dead feed can't kill the run
            print(f"feed {name} error: {e}", file=sys.stderr); continue
        for title, desc, link in items:
            blob = f"{title}. {desc}".strip()
            if PROMO_RX.search(blob):          # sportsbook ad, not news — drop
                dropped += 1
                continue
            read = cold_read(blob, vocab_weight, tok_to_games)
            relevance = ("HOT" if (read["score"] >= HOT_SCORE and read["topDoc"] and read["bridges"])
                         else "WARM" if read["score"] >= WARM_SCORE
                         else "COLD")
            if relevance == "COLD":
                dropped += 1
                continue
            fac = factors(blob)
            sha = _sha(blob)
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
            item = {
                "title": title, "desc": desc[:400], "url": link, "feed": name,
                "sha": sha, "relevance": relevance, "score": read["score"],
                "mo": {"trigger": read["trigger"], "topDoc": read["topDoc"],
                       "bridges": read["bridges"]},
                "factors": fac,
                "sentiment": sentiment(blob, team_of, read["hooks"]),
                "half_life_h": half_life(fac),
            }
            kept.append(item)
            tag = f"{relevance:4} {read['score']:.2f}"
            sent = item["sentiment"]
            slabel = f" · {sent['tag']}({sent['cue']})" if sent["cue"] else ""
            print(f"+ [{tag}] {'/'.join(fac):16} {read['topDoc'] or '—':>18} | "
                  f"{title[:52]}{slabel}", file=sys.stderr)

    # rank: relevance first, then score, then it's already fresh (this pass)
    kept.sort(key=lambda x: (x["relevance"] != "HOT", -x["score"]))

    # optional: extract HOT/WARM into the decaying bin (the expensive lane)
    extracted = 0
    if do_extract and kept:
        for item in kept:
            blob = f"{item['title']}. {item['desc']}".strip()
            try:
                cell = Cell(text=blob, extraction=extract(blob), ts=_now_iso(),
                            source={"type": item["factors"][0],
                                    "half_life_h": item["half_life_h"],
                                    "url": item["url"], "feed": item["feed"],
                                    "relevance": item["relevance"], "mo": item["mo"],
                                    "sentiment": item["sentiment"]}).stamped()
                append_to_bin(cell)
                extracted += 1
            except Exception as e:
                print(f"  extract failed ({item['sha']}): {e}", file=sys.stderr)
        live = sweep_bin()
        print(f"extracted {extracted} cells · bin holds {live} live", file=sys.stderr)

    out = {"generated_at": _now_iso(), "slate_date": slate_date,
           "games": len(games), "kept": len(kept), "dropped": dropped,
           "extracted": extracted, "items": kept}
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n{len(kept)} kept ({sum(i['relevance']=='HOT' for i in kept)} HOT / "
          f"{sum(i['relevance']=='WARM' for i in kept)} WARM) · {dropped} dropped as noise\n"
          f"→ {out_path}", file=sys.stderr)
    return out


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main(argv):
    do_extract = "--extract" in argv
    out_path = OUT_DEFAULT
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]
    run(do_extract, out_path)


if __name__ == "__main__":
    main(sys.argv[1:])
