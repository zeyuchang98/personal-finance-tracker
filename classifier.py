"""Classification: rule library first, Claude (guide-primed) fallback.

Fast path: substring-containment match on the normalized merchant name uses the
saved rule (free, deterministic). Normalization strips store numbers so
"WHOLEFDS #123" and "WHOLEFDS #456" share a rule. Matching is hardened against
the classic substring pitfalls: the LONGEST matching pattern wins (most specific,
order-independent) and patterns shorter than MIN_PATTERN_LEN are ignored so a
tiny pattern can't blanket everything.

Fallback: misses go to Claude, primed with categorization_guide.md (an editable
plain-English guide) so edge cases get real reasoning instead of brittle string
matching. Claude may ONLY pick from the current candidate set and never invents
a category; it falls back to Other otherwise.
"""
import json
import os
import re

from db import get_rules, get_categories, OTHER, IGNORE

_GUIDE_PATH = os.path.join(os.path.dirname(__file__), "categorization_guide.md")
_GUIDE_EXAMPLE = os.path.join(os.path.dirname(__file__), "categorization_guide.example.md")


def ensure_guide() -> None:
    """Create categorization_guide.md from the bundled example on first run.
    The real guide is git-ignored (it accumulates personal rules), so a fresh
    clone starts from the clean template."""
    if not os.path.exists(_GUIDE_PATH) and os.path.exists(_GUIDE_EXAMPLE):
        import shutil
        shutil.copy(_GUIDE_EXAMPLE, _GUIDE_PATH)


def _load_guide() -> str:
    try:
        with open(_GUIDE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

# ---------- merchant normalization ----------

# Noise stripped from merchant names before matching/keying:
#   #123          store-number suffixes
#   \d{3,}        long pure-digit runs (store IDs)
#   long ref code an 8+ char alphanumeric token with >=2 digits mixed in --
#                 bank/Zelle/ACH reference codes like "JPM99CMBCYHY" that differ
#                 on every payment to the same person and would otherwise make
#                 each transaction a distinct merchant (inconsistent labels).
#                 Length 8 + two digits avoids real names like 7ELEVEN / M1 / B12.
#   \*+           asterisks
_NOISE = re.compile(
    r"#\s*\d+|\b\d{3,}\b|\b(?=[A-Z0-9]*\d[A-Z0-9]*\d)[A-Z0-9]{8,}\b|\*+|\s{2,}")


def normalize_merchant(raw: str) -> str:
    """Strip store numbers, long digit runs, bank reference codes, asterisks;
    uppercase; collapse spaces."""
    if not raw:
        return ""
    s = raw.upper()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


MIN_PATTERN_LEN = 4  # ignore very short patterns to avoid blanket over-matching


def match_rule(normalized: str, rules) -> str | None:
    """Substring containment match; longest matching pattern wins.

    Longest-match makes the result order-independent and prefers the most
    specific rule (e.g. "AMAZON PRIME" beats "AMAZON"). Patterns shorter than
    MIN_PATTERN_LEN are skipped so a stray short rule can't match everything.
    """
    if not normalized:
        return None
    best_cat, best_len = None, 0
    for pattern, category in rules:
        p = normalize_merchant(pattern)
        if len(p) >= MIN_PATTERN_LEN and p in normalized and len(p) > best_len:
            best_cat, best_len = category, len(p)
    return best_cat


# ---------- Claude fallback ----------

# Max merchants per Claude call. A real first sync can have 1000+ distinct
# merchants; one giant request would blow past max_tokens and the truncated
# JSON reply would silently classify everything as Other.
_CHUNK_SIZE = 40


# A classification unit. Direction is part of the key because the same merchant
# at the same amount means different things inflow vs outflow (a refund coming
# back vs. a purchase going out).
Key = tuple[str, float, str]  # (normalized_merchant, amount, "in"|"out")


def direction_of(amount) -> str:
    """Plaid: positive amount = money out, negative = money in."""
    return "in" if float(amount) < 0 else "out"


def key_of(row: dict) -> Key:
    return (row["merchant_normalized"], row["amount"], direction_of(row["amount"]))


def _claude_classify(rows: list[dict], candidates: list[str]) -> dict[Key, str]:
    """Batch-classify transaction rows, chunked to keep each Claude reply
    comfortably inside max_tokens. Returns {key: label}."""
    out: dict[Key, str] = {}
    for i in range(0, len(rows), _CHUNK_SIZE):
        out.update(_claude_classify_chunk(rows[i:i + _CHUNK_SIZE], candidates))
    return out


def _claude_classify_chunk(rows: list[dict], candidates: list[str]) -> dict[Key, str]:
    """Classify one chunk of rows. Each row is shown to Claude with its amount,
    direction (in/out), account type and Plaid category hint, so Claude can pick
    not just a spending category but also money-movement labels (Income,
    Transfer, Investment, CC Payment, Cash, Refund). Returns {key: label}."""
    if not rows:
        return {}
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    cand_str = "\n".join(f"- {c}" for c in candidates)
    lines = []
    for i, r in enumerate(rows):
        amt = float(r["amount"])
        d = "money OUT" if amt >= 0 else "money IN"
        acct = r.get("account_type") or "?"
        pfc = (r.get("pfc") or "").strip()
        pfc_str = f", plaid={pfc}" if pfc else ""
        lines.append(f"{i}. {r['merchant_normalized']}  "
                     f"[{d}, ${abs(amt):.2f}, {acct} account{pfc_str}]")
    merch_str = "\n".join(lines)
    guide = _load_guide()
    guide_block = f"\nCategorization guide (follow these preferences):\n{guide}\n" if guide else ""
    prompt = (
        "You classify bank/card transactions. Each line is a merchant name plus, "
        "in brackets, the direction (money OUT = a charge you paid, money IN = a "
        "deposit/credit), the amount, the account type, and Plaid's category hint.\n"
        "Pick the single best label from this exact list (copy it verbatim):\n"
        f"{cand_str}\n\n"
        "Guidance: most 'money OUT' rows are spending categories. Use the "
        "money-movement labels when they fit: Transfer (moving your own money "
        "between accounts), Investment (brokerage/retirement contributions), "
        "CC Payment (paying a credit-card bill), Cash (ATM withdrawals), Salary "
        "(employment payroll coming in), Other Income (any other inflow: interest, "
        "dividends, tax refunds, deposits), Refund (a merchant returning money "
        "for a prior purchase). The amount is a secondary signal to disambiguate "
        "when the guide calls for it.\n"
        f"If nothing clearly fits, use \"{OTHER}\". Do NOT invent labels.\n"
        f"{guide_block}\n"
        "Transactions:\n"
        f"{merch_str}\n\n"
        'Reply with ONLY a JSON array of objects: '
        '[{"i": <index>, "category": "<one label string>"}]'
    )
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0,  # deterministic: same transaction -> same label across runs
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        text = m.group(0)
    valid = set(candidates)
    out: dict[Key, str] = {}
    try:
        for obj in json.loads(text):
            idx = obj.get("i")
            cat = obj.get("category")
            if isinstance(idx, int) and 0 <= idx < len(rows):
                out[key_of(rows[idx])] = cat if cat in valid else OTHER
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    for r in rows:  # anything Claude omitted -> Other
        out.setdefault(key_of(r), OTHER)
    return out


def draft_category_meaning(name: str, existing: list[str]) -> str:
    """One concise plain-English sentence describing what belongs in a new
    spending category, to seed the guide so classification knows when to pick
    it. Best-effort: returns '' if the API call fails."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        others = ", ".join(c for c in existing if c != name) or "(none yet)"
        prompt = (
            "A personal-finance app is adding a new spending category. Write ONE "
            "concise sentence describing what kinds of merchants or charges belong "
            "in it, so a classifier knows when to pick it. Keep it distinct from "
            "the existing categories.\n"
            f"New category: {name}\n"
            f"Existing categories: {others}\n"
            "Reply with ONLY the sentence — no label, no category name prefix, no quotes."
        )
        resp = client.messages.create(
            model=model, max_tokens=120, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return ""


def classify_batch(rows: list[dict]) -> dict[Key, tuple[str, str]]:
    """Classify a batch of transaction rows. Each row needs merchant_normalized
    and amount (and, for the Claude path, account_type + pfc).

    Returns {key: (label, classified_by)} keyed on (merchant, amount, direction),
    where classified_by is 'rule' or 'claude'. EVERY transaction is classified
    now -- spending and money-movement alike. The rule fast-path is name-based
    (a saved merchant->label rule wins regardless of amount); the Claude fallback
    is context-aware (amount + direction + account + pfc). Deduplicates by key so
    Claude is called once per distinct (merchant, amount, direction).
    """
    rules = get_rules()
    # All labels except Ignore (manual-only: Claude must never auto-ignore a row).
    candidates = [c for c in get_categories() if c != IGNORE]
    result: dict[Key, tuple[str, str]] = {}
    unmatched: dict[Key, dict] = {}  # key -> a representative row for context

    for r in rows:
        key = key_of(r)
        if key in result or key in unmatched:
            continue
        cat = match_rule(r["merchant_normalized"], rules)
        if cat is not None:
            result[key] = (cat, "rule")
        else:
            unmatched[key] = r

    if unmatched:
        claude_out = _claude_classify(list(unmatched.values()), candidates)
        for key in unmatched:
            result[key] = (claude_out.get(key, OTHER), "claude")

    return result
