"""Deterministic editorial checks for deep reads and index lines (docs/editorial-style.md).

No model is involved: every check is a pattern over the draft text and the style data in
data/editorial-style.json. A draft that fails goes back to its writer with the findings
(at most `thresholds.max_rewrites` times); an item that still fails is demoted to a
one-line index entry.

A day's draft is one Markdown file per language:

    ## <deep heading>
    ### 1. <title>
    Source: <link> ...
    **<Bottom line>** text
    **<What is new>** text
    **<Limits>** text
    **<Our take>** text
    ### 2. ...
    ## <index heading>
    - <title>: one sentence.

The four bold labels per language are `deep_parts` in the style data. Headings other than
"### n." are free text; the checker only relies on the "### " prefix for items and the
"- " prefix for index lines after the first "## " heading that follows the deep items.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import statistics
import unicodedata

STYLE_PATH = Path(__file__).resolve().parent.parent / "data" / "editorial-style.json"
LANGS = ("zh", "en", "ja")
_WS = re.compile(r"\s+")
_NUMBER = r"\d+(?:[.,]\d+)?"
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
# A sentence ends at ., !, ? (followed by space or end) or at the CJK stops. Decimal
# points and "e.g." do not end one: a terminal period must be followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[。！？])|(?<=[.!?])(?=\s)")
_ABBREVIATION = re.compile(r"(?:\b(?:Sec|Secs|Fig|Figs|Tab|App|Eq|Eqs|No|vs|al|e\.g|i\.e|cf|approx|p|pp|ca)\.)$", re.I)


def load_style(path: Path = STYLE_PATH) -> dict:
    style = json.loads(Path(path).read_text(encoding="utf-8"))
    if style.get("schema") != "pairadar-editorial-style-1":
        raise ValueError("editorial style schema differs")
    return style


def normalise(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END.split(normalise(text))]
    merged: list[str] = []
    for part in parts:
        if not part:
            continue
        # "Sec. 3.2", "Fig. 4", "et al. 2025": an abbreviation's period does not end a sentence.
        if merged and _ABBREVIATION.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def text_length(sentence: str, lang: str) -> int:
    """Words for English, characters for Chinese and Japanese (spaces dropped)."""
    if lang == "en":
        return len(sentence.split())
    return len(sentence.replace(" ", ""))


@dataclass
class Deep:
    title: str
    parts: dict[str, str]          # canonical part index ("0".."3") -> text
    raw: str


@dataclass
class Draft:
    lang: str
    deep: list[Deep] = field(default_factory=list)
    index: list[str] = field(default_factory=list)


def parse(markdown: str, lang: str, style: dict) -> Draft:
    labels = style["deep_parts"][lang]
    label_re = re.compile(r"^\*\*(" + "|".join(re.escape(label) for label in labels) + r")\*\*[:：]?\s*(.*)$")
    draft = Draft(lang)
    current: Deep | None = None
    part: str | None = None
    in_index = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            current, part, in_index = Deep(stripped[4:].strip(), {}, ""), None, False
            draft.deep.append(current)
            continue
        if stripped.startswith("## "):
            if draft.deep:
                in_index, current, part = True, None, None
            continue
        if in_index:
            if stripped.startswith("- "):
                draft.index.append(stripped[2:].strip())
            continue
        if current is None:
            continue
        current.raw += line + "\n"
        match = label_re.match(stripped)
        if match:
            part = str(labels.index(match.group(1)))
            current.parts[part] = match.group(2).strip()
        elif part is not None and stripped:
            current.parts[part] = (current.parts[part] + " " + stripped).strip()
    return draft


def banned_hits(text: str, lang: str, style: dict) -> list[str]:
    folded = normalise(text).casefold()
    return [phrase for phrase in style["banned"][lang] if normalise(phrase).casefold() in folded]


def has_emoji(text: str, style: dict) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in style["emoji_ranges"])


def _unit_pattern(lang: str, style: dict) -> re.Pattern:
    units = sorted(style["number_units"]["all"] + style["number_units"].get(lang, []), key=len, reverse=True)
    latin = [re.escape(u) for u in units if not _CJK.search(u)]
    cjk = [re.escape(u) for u in units if _CJK.search(u)]
    alternatives = []
    if latin:
        # A Latin unit must end the token: "72.6 m" is metres, "72.6 matches" is not.
        alternatives.append(r"\s?(?:" + "|".join(latin) + r")(?![A-Za-z])")
    if cjk:
        alternatives.append(r"\s?(?:" + "|".join(cjk) + r")")
    return re.compile(_NUMBER + "(?:" + "|".join(alternatives) + ")")


def triples(text: str, lang: str, style: dict) -> list[str]:
    """Sentences that carry a measured number, a baseline and a source locator.

    A measured number is a number with a unit ("72.6%", "41.15 px") or a named statistic
    ("r = +0.681", "ρ = 0.53", "LPIPS 0.13" is not enough, "LPIPS = 0.13" is)."""
    units = _unit_pattern(lang, style)
    statistic = re.compile(style["statistics"])
    locator = re.compile(style["locators"])   # case-sensitive on purpose: "page is" is not a locator
    markers = [m.casefold() for m in style["baseline_markers"][lang] + style["baseline_markers"]["en"]]
    found = []
    for sentence in sentences(text):
        folded = " " + sentence.casefold() + " "
        measured = units.search(sentence) or statistic.search(sentence)
        if measured and locator.search(sentence) and any(m in folded for m in markers):
            found.append(sentence)
    return found


def first_sentence_is_concrete(text: str, lang: str, style: dict) -> bool:
    """The bottom line opens with a number and a comparison, not with background."""
    first = (sentences(text) or [""])[0]
    folded = " " + first.casefold() + " "
    markers = [m.casefold() for m in style["baseline_markers"][lang] + style["baseline_markers"]["en"]]
    return bool(re.search(_NUMBER, first)) and any(m in folded for m in markers)


def _shingles(sentence: str, lang: str, size: int) -> set[str]:
    clean = re.sub(_NUMBER, "#", normalise(sentence).casefold())
    if lang == "en":
        words = re.findall(r"[a-z#]+", clean)
        return {" ".join(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}
    chars = re.sub(r"\s", "", clean)
    return {chars[i:i + size] for i in range(max(0, len(chars) - size + 1))}


def template_rate(texts: list[str], lang: str) -> tuple[float, list[str]]:
    """Share of sentences whose wording mostly recurs in another item of the same day.

    A sentence counts as templated when at least half of its shingles (4 words, or 6
    characters for Chinese/Japanese, digits folded) also occur in a different item. That
    catches the same skeleton with only the numbers or names swapped.
    """
    size = 4 if lang == "en" else 6
    per_item = [[(s, _shingles(s, lang, size)) for s in sentences(t)] for t in texts]
    bags = [set().union(*(sh for _, sh in item)) if item else set() for item in per_item]
    templated, total = [], 0
    for i, item in enumerate(per_item):
        others = set().union(*(bag for j, bag in enumerate(bags) if j != i)) if len(bags) > 1 else set()
        for sentence, shingles in item:
            if not shingles:
                continue
            total += 1
            if len(shingles & others) * 2 >= len(shingles):
                templated.append(sentence)
    return (len(templated) / total if total else 0.0), templated


def length_cv(text: str, lang: str) -> float:
    lengths = [text_length(s, lang) for s in sentences(text)]
    if len(lengths) < 3 or statistics.mean(lengths) == 0:
        return 1.0   # too few sentences to judge rhythm; other checks cover thin items
    return statistics.pstdev(lengths) / statistics.mean(lengths)


def check_deep(deep: Deep, lang: str, style: dict) -> list[str]:
    findings = []
    labels = style["deep_parts"][lang]
    missing = [labels[int(i)] for i in "0123" if not deep.parts.get(i, "").strip()]
    if missing:
        findings.append(f"missing part(s): {', '.join(missing)}")
    body = " ".join(deep.parts.get(i, "") for i in "0123")
    hits = banned_hits(deep.title + " " + deep.raw, lang, style)
    if len(hits) > style["thresholds"]["max_banned_hits"]:
        findings.append(f"stock phrase(s): {', '.join(hits)}")
    if has_emoji(deep.raw, style):
        findings.append("emoji")
    if len(triples(body, lang, style)) < style["thresholds"]["min_triples_per_deep"]:
        findings.append("no sentence with a number + unit, its baseline and a source locator (Table/Fig./Sec./p.)")
    if deep.parts.get("0") and not first_sentence_is_concrete(deep.parts["0"], lang, style):
        findings.append(f"{labels[0]}: first sentence has no number with a comparison")
    limit = deep.parts.get("2", "")
    if limit and len(normalise(limit).replace(" ", "") if lang != "en" else normalise(limit)) \
            < style["limits"]["min_chars"][lang]:
        findings.append(f"{labels[2]}: too thin to state the scope (sim/real, sample size, release)")
    if length_cv(body, lang) < style["thresholds"]["min_sentence_length_cv"]:
        findings.append("sentence lengths barely vary")
    return findings


def index_sentence(line: str) -> str:
    """The sentence of an index line, without its leading title ("<title>: sentence")."""
    for separator in ("：", ": "):
        if separator in line:
            return line.split(separator, 1)[1].strip()
    return line


def check_index(line: str, lang: str, style: dict) -> list[str]:
    findings = []
    hits = banned_hits(line, lang, style)
    if hits:
        findings.append(f"stock phrase(s): {', '.join(hits)}")
    if has_emoji(line, style):
        findings.append("emoji")
    # 05 (pilot day 1): an index line must still let the reader open the source and see its evidence grade.
    if not re.search(r"\[[^\]]+\]\(https?://[^)\s]+\)", line):
        findings.append("index line has no link to its source")
    if not re.match(r"^`?\[(?:O|R|M)\]`?\s", line):
        findings.append("index line does not start with its evidence tag [O]/[R]/[M]")
    after_link = re.search(r"\]\(https?://[^)\s]+\)[:：]?\s*", line)
    sentence = line[after_link.end():] if after_link else index_sentence(line)
    if lang == "en":
        if len(sentence.split()) > style["index"]["max_words_en"]:
            findings.append("index line too long")
    elif len(sentence.replace(" ", "")) > style["index"]["max_chars_cjk"]:
        findings.append("index line too long")
    return findings


def check(markdown: str, lang: str, style: dict | None = None, deep_count: int = 3,
          index_count: int = 5) -> dict:
    """Check one language's draft. Returns findings per item and the day's metrics."""
    style = style or load_style()
    if lang not in LANGS:
        raise ValueError(f"unsupported language: {lang}")
    draft = parse(markdown, lang, style)
    report = {"lang": lang, "deep": [], "index": [], "day": []}
    for deep in draft.deep:
        findings = check_deep(deep, lang, style)
        report["deep"].append({"title": deep.title, "findings": findings,
                               "triples": len(triples(" ".join(deep.parts.values()), lang, style)),
                               "banned": banned_hits(deep.raw, lang, style)})
    for line in draft.index:
        report["index"].append({"line": line[:80], "findings": check_index(line, lang, style)})
    if len(draft.deep) != deep_count:
        report["day"].append(f"{len(draft.deep)} deep read(s), expected {deep_count}")
    if len(draft.index) != index_count:
        report["day"].append(f"{len(draft.index)} index line(s), expected {index_count}")
    rate, templated = template_rate([" ".join(d.parts.values()) for d in draft.deep] + draft.index, lang)
    report["template_rate"] = round(rate, 3)
    if rate > style["thresholds"]["max_template_rate"]:
        report["day"].append(f"template rate {rate:.0%} above {style['thresholds']['max_template_rate']:.0%}: "
                             + " | ".join(t[:60] for t in templated[:3]))
    report["banned_total"] = sum(len(d["banned"]) for d in report["deep"]) + sum(
        len(banned_hits(line, lang, style)) for line in draft.index)
    report["passed"] = not report["day"] and all(not d["findings"] for d in report["deep"]) and all(
        not i["findings"] for i in report["index"])
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check a deep-read draft against docs/editorial-style.md")
    parser.add_argument("draft", type=Path)
    parser.add_argument("--lang", required=True, choices=LANGS)
    parser.add_argument("--deep", type=int, default=3)
    parser.add_argument("--index", type=int, default=5)
    args = parser.parse_args(argv)
    report = check(args.draft.read_text(encoding="utf-8"), args.lang, deep_count=args.deep, index_count=args.index)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
