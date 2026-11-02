"""Property tests for the two places ADR 029 asks to be proven by construction rather than by examples:
robots.txt matching (RFC 9309) and "nothing is cut silently". Seeded, standard library only, no network.

Each property compares the module with an independent restatement of the rule (a regular-expression matcher, a size
count), over many generated inputs concentrated where the rules change: equal lengths, the limit and one past it.
"""
from __future__ import annotations

import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import robots_rules as rr  # noqa: E402
from pairadar import static_extract as se  # noqa: E402

TOKEN = "physical-ai-radar"
SEED = 20261102


# --- robots.txt ---------------------------------------------------------------------------------------

def reference_allowed(rules: list[tuple[bool, str]], target: str) -> bool:
    """RFC 9309 restated: the longest matching pattern wins, Allow on a tie, no match allows."""
    best_length, best_allow = -1, True
    for allow, pattern in rules:
        anchored = pattern.endswith("$")
        core = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in core.split("*")) + (r"\Z" if anchored else "")
        if re.match(regex, target, re.S):
            if len(pattern) > best_length or (len(pattern) == best_length and allow):
                best_length, best_allow = len(pattern), allow
    return best_allow


def random_pattern(rng: random.Random) -> str:
    body = "".join(rng.choice("ab/?x*") for _ in range(rng.randint(1, 6)))
    return "/" + body + ("$" if rng.random() < 0.25 else "")


def random_target(rng: random.Random) -> str:
    """A request path the fetcher would really send: an empty query (`/a?`) is dropped from the URL, so it never appears."""
    target = "/" + "".join(rng.choice("ab/?x") for _ in range(rng.randint(0, 8)))
    return target[:-1] if target.find("?") == len(target) - 1 else target


def random_file(rng: random.Random):
    """(text, rules that apply to us). Every group has at least one rule line, so no two groups run together."""
    groups = []
    for _ in range(rng.randint(1, 5)):
        agents = rng.sample(["*", TOKEN, "Physical-AI-Radar", "other", "physical-ai-radar/1.0", "not-physical-ai-radar"], rng.randint(1, 3))
        rules = [(rng.random() < 0.5, random_pattern(rng)) for _ in range(rng.randint(1, 4))]
        groups.append((agents, rules))
    lines = []
    for agents, rules in groups:
        for agent in agents:
            lines.append(rng.choice(["User-agent: ", "user-agent:", "USER-AGENT : "]) + agent)
            if rng.random() < 0.2:
                lines.append(rng.choice(["# comment", "", "Sitemap: https://good.test/s.xml", "Crawl-delay: 5"]))
        for allow, pattern in rules:
            lines.append(("Allow: " if allow else "Disallow: ") + pattern + rng.choice(["", "  # why"]))
            if rng.random() < 0.2:
                lines.append(rng.choice(["# comment", "", "Sitemap: https://good.test/s.xml"]))
        lines.append(rng.choice(["", "# next group"]))
    ours = [rule for agents, rules in groups if any(a.lower() == TOKEN or a.lower().startswith(TOKEN + "/") for a in agents) for rule in rules]
    star = [rule for agents, rules in groups if "*" in agents for rule in rules]
    return rng.choice(["\n", "\r\n"]).join(lines) + "\n", ours or star


class RobotsProperties(unittest.TestCase):
    def test_the_matcher_agrees_with_the_restated_rule(self):
        rng = random.Random(SEED)
        undecided = 0
        for _ in range(1500):
            text, rules = random_file(rng)
            parsed = rr.parse(text, TOKEN)
            expected_rules = [(allow, rr.normalise(pattern)) for allow, pattern in rules]
            self.assertEqual(sorted(parsed.rules), sorted(expected_rules), text)           # the right rules were chosen
            for _ in range(8):
                target = random_target(rng)
                want = reference_allowed(expected_rules, rr.normalise(target))
                self.assertEqual(parsed.allowed("https://good.test" + target), want, (text, target))
                undecided += not want
        self.assertGreater(undecided, 200)                       # the generator really produces refusals

    def test_the_order_of_rules_never_matters(self):
        rng = random.Random(SEED + 1)
        for _ in range(400):
            rules = [(rng.random() < 0.5, random_pattern(rng)) for _ in range(rng.randint(2, 8))]
            shuffled = rules[:]
            rng.shuffle(shuffled)
            a = rr.Rules([(allow, rr.normalise(p)) for allow, p in rules])
            b = rr.Rules([(allow, rr.normalise(p)) for allow, p in shuffled])
            for _ in range(6):
                target = "https://good.test" + random_target(rng)
                self.assertEqual(a.allowed(target), b.allowed(target))

    def test_splitting_or_joining_groups_of_one_agent_changes_nothing(self):
        rng = random.Random(SEED + 2)
        for _ in range(300):
            rules = [(rng.random() < 0.5, random_pattern(rng)) for _ in range(rng.randint(2, 6))]
            line = lambda r: ("Allow: " if r[0] else "Disallow: ") + r[1]
            joined = f"User-agent: {TOKEN}\n" + "\n".join(line(r) for r in rules) + "\n"
            cut = rng.randint(1, len(rules) - 1)
            split = (f"User-agent: {TOKEN}\n" + "\n".join(line(r) for r in rules[:cut]) + "\n\nUser-agent: other\nDisallow: /zzz\n\n"
                     f"User-agent: {TOKEN}\n" + "\n".join(line(r) for r in rules[cut:]) + "\n")
            a, b = rr.parse(joined, TOKEN), rr.parse(split, TOKEN)
            for _ in range(6):
                target = "https://good.test" + random_target(rng)
                self.assertEqual(a.allowed(target), b.allowed(target))

    def test_adding_a_rule_can_only_move_the_decision_by_that_rule(self):
        """A longer Disallow that matches always refuses; a longer Allow that matches always allows."""
        rng = random.Random(SEED + 3)
        for _ in range(500):
            rules = [(rng.random() < 0.5, rr.normalise(random_pattern(rng))) for _ in range(rng.randint(0, 5))]
            target = random_target(rng)
            normal = rr.normalise(target)
            longest = max((len(p) for _, p in rules), default=0)
            exact = normal + "$"
            if len(exact) > longest:
                self.assertFalse(rr.Rules(rules + [(False, exact)]).allowed("https://good.test" + target))
                self.assertTrue(rr.Rules(rules + [(True, exact)]).allowed("https://good.test" + target))

    def test_special_characters_and_their_escapes_agree_with_the_restated_rule(self):
        """One Disallow rule, rule and URL built from tokens that can be spelled as a character or as an escape."""
        rng = random.Random(SEED + 9)
        rule_tokens = ["a", "b", "A", "/", "x", "*", "%2A", "%2a", "%24", "%41", "%61", "$"]
        target_tokens = ["a", "b", "A", "/", "x", "*", "$", "%2A", "%2a", "%24", "%41", "%61"]

        def data(token: str) -> str:
            return {"%2A": "*", "%2a": "*", "%24": "$", "%41": "A", "%61": "a"}.get(token, token)

        def restated(rule: list[str], target: list[str]) -> bool:
            """Syntax: a literal `*` is a wildcard, a literal `$` last is the end; everything else, escaped or not, is data."""
            anchored = bool(rule) and rule[-1] == "$"
            body = rule[:-1] if anchored else rule
            regex = "".join(".*" if t == "*" else re.escape(data(t)) for t in body) + (r"\Z" if anchored else "")
            return re.match(regex, "".join(data(t) for t in target), re.S) is not None

        refused = 0
        for _ in range(3000):
            rule = ["/"] + [rng.choice(rule_tokens) for _ in range(rng.randint(0, 6))]
            target = ["/"] + [rng.choice(target_tokens) for _ in range(rng.randint(0, 6))]
            got = not rr.Rules([(False, rr.normalise("".join(rule)))]).allowed("https://good.test" + "".join(target))
            self.assertEqual(got, restated(rule, target), (rule, target))
            refused += got
        self.assertGreater(refused, 150)                                                       # both outcomes really occur

    def test_priority_counts_the_same_octets_whatever_the_spelling(self):
        """An Allow and a Disallow, each spelled with characters or escapes: the winner is the longer one in octets."""
        rng = random.Random(SEED + 10)
        tokens = ["a", "b", "A", "/", "x", "*", "%2A", "%24", "%41", "%61", "$"]

        def data(token: str) -> str:
            return {"%2A": "*", "%24": "$", "%41": "A", "%61": "a"}.get(token, token)

        def matches(rule: list[str], target: list[str]) -> bool:
            anchored = rule[-1] == "$" and len(rule) > 1
            body = rule[:-1] if anchored else rule
            regex = "".join(".*" if t == "*" else re.escape(data(t)) for t in body) + (r"\Z" if anchored else "")
            return re.match(regex, "".join(data(t) for t in target), re.S) is not None

        decided = {True: 0, False: 0}
        for _ in range(4000):
            allow = ["/"] + [rng.choice(tokens) for _ in range(rng.randint(0, 5))]
            disallow = ["/"] + [rng.choice(tokens) for _ in range(rng.randint(0, 5))]
            target = ["/"] + [rng.choice(tokens[:5] + ["$", "%24"]) for _ in range(rng.randint(0, 6))]
            best, want = -1, True
            for is_allow, rule in ((True, allow), (False, disallow)):
                if matches(rule, target) and (len(rule) > best or (len(rule) == best and is_allow)):
                    best, want = len(rule), is_allow            # one token is one octet; a wildcard and an end anchor count as one
            rules = rr.Rules([(True, rr.normalise("".join(allow))), (False, rr.normalise("".join(disallow)))])
            self.assertEqual(rules.allowed("https://good.test" + "".join(target)), want, (allow, disallow, target))
            if best >= 0:
                decided[want] += 1
        self.assertGreater(decided[True], 200)
        self.assertGreater(decided[False], 200)

    def test_percent_encoding_is_one_form(self):
        rng = random.Random(SEED + 4)
        pieces = ["a", "b", "/", "~", "-", "%41", "%61", "%7E", "%2F", "%2A", "%C3%A9", "é", "*", "?q", "%25"]
        for _ in range(800):
            parts = [rng.choice(pieces) for _ in range(rng.randint(1, 7))]
            once = rr.normalise("".join(parts))
            self.assertEqual(rr.normalise(once), once)                                   # idempotent
            lowered = rr.normalise("".join(re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), p) for p in parts))
            self.assertEqual(lowered, once)                                              # the case of an escape never matters
            self.assertTrue(once.isascii())
        # whatever else is put into the text, a % that starts no escape is refused: nothing is ever "completed" by decoding
        stray = ["%", "%4", "%zz", "%G1", "%-"]
        for _ in range(800):
            parts = [rng.choice(pieces + stray) for _ in range(rng.randint(1, 7))]
            text = "".join(parts)
            invalid = re.search(r"%(?![0-9A-Fa-f]{2})", text) is not None
            if invalid:
                with self.assertRaises(rr.RobotsUnsupported):
                    rr.normalise(text)
            else:
                rr.normalise(text)

    def test_a_file_is_either_judged_whole_or_refused(self):
        rng = random.Random(SEED + 5)
        for count in (rr.MAX_RULES - 1, rr.MAX_RULES, rr.MAX_RULES + 1, rr.MAX_RULES + 7):
            text = "User-agent: *\n" + "".join(f"Disallow: /p{i}\n" for i in range(count))
            if count > rr.MAX_RULES:
                with self.assertRaises(rr.RobotsUnsupported):
                    rr.parse(text, TOKEN)
            else:
                self.assertEqual(len(rr.parse(text, TOKEN).rules), count)                   # nothing dropped on the way
        for length in (rr.MAX_PATTERN - 1, rr.MAX_PATTERN, rr.MAX_PATTERN + 1):
            text = "User-agent: *\nDisallow: /" + "a" * (length - 1) + "\n"
            if length > rr.MAX_PATTERN:
                with self.assertRaises(rr.RobotsUnsupported):
                    rr.parse(text, TOKEN)
            else:
                self.assertEqual(len(rr.parse(text, TOKEN).rules), 1)
        self.assertTrue(rng)                                                               # (the seed is kept for symmetry)


# --- nothing is cut silently --------------------------------------------------------------------------

def page(lines: list[str], links: int) -> tuple[str, int]:
    """(html, markdown length the extractor must produce): one paragraph per line, then `links` link paragraphs."""
    body = "".join(f"<p>{line}</p>" for line in lines)
    link_lines = [f"[Post {i} title](/news/p{i})" for i in range(links)]
    body += "".join(f"<p><a href='/news/p{i}'>Post {i} title</a></p>" for i in range(links))
    markdown = "\n".join(lines + link_lines)
    return f"<html><head><title>T</title></head><body><main>{body}</main></body></html>", len(markdown)


class UrlProperties(unittest.TestCase):
    """The invariant behind the path checks: a URL parse_url accepts is judged by robots.txt exactly as every server
    resolution of it would be. The server models are mine and are not claims about any real server."""

    @staticmethod
    def resolve(path: str) -> str:
        out: list[str] = []
        segments = path.split("/")
        for index, segment in enumerate(segments[1:], start=1):
            if segment == ".":
                continue
            if segment == "..":
                if out:
                    out.pop()
                continue
            out.append(segment)
        return "/" + "/".join(out)

    def test_every_spelling_of_a_word_reads_the_same_to_robots_at_every_decoding_depth(self):
        """01's R-D-155c-01 and -02: `/%2570rivate` is `/private` after two decodings; `/private;v=1` is `/private` to a
        server that strips path parameters. For every accepted spelling the verdict must not move with the model."""
        import urllib.parse
        from pairadar import static_fetch as sf
        rng = random.Random(SEED + 12)
        rule_sets = [rr.parse(text, TOKEN) for text in (
            "User-agent: *\nDisallow: /private\n", "User-agent: *\nDisallow: /private$\n", "User-agent: *\nAllow: /public\nDisallow: /private\n",
            "User-agent: *\nDisallow: /priv\nAllow: /private\n", "User-agent: *\nDisallow: /*vat\n", "User-agent: *\nDisallow: /p*e$\n",
            "User-agent: *\nAllow: /\nDisallow: /pr\n")]

        def spelling(letter: str) -> str:
            code = ord(letter)
            return rng.choice([letter] * 8 + [f"%{code:02x}", f"%{code:02X}", f"%25{code:02x}"])

        def models(raw: str):
            decoded = raw
            for _ in range(4):
                yield decoded
                yield re.sub(r";[^/]*", "", decoded)                                # a server that strips path parameters
                yield decoded.replace("\\", "/")
                decoded = urllib.parse.unquote(decoded)

        accepted = rejected = moved_without_guard = 0
        for _ in range(4000):
            word = rng.choice(["private", "public", "priv", "privates", "pre"])
            suffix = rng.choice(["", "", "/", "/x", ";v=1", "%3Bv=1", "%3b", ";", "/;x", "%2F"])
            path = "/" + "".join(spelling(letter) for letter in word) + suffix
            try:
                sf.parse_url("https://good.test" + path)
            except sf.StaticFetchError:
                rejected += 1
                # the rejected ones are exactly where some model disagrees with the raw reading for at least one rule set
                continue
            accepted += 1
            for rules in rule_sets:
                want = rules.allowed("https://good.test" + path)
                for model in models(path):
                    try:
                        got = rules.allowed("https://good.test" + model)
                    except rr.RobotsUnsupported:
                        continue
                    self.assertEqual(got, want, (path, model))
        self.assertGreater(accepted, 400)
        self.assertGreater(rejected, 400)
        # and the guard is not vacuous: without it the models do disagree on spellings it refuses
        for path in ("/%2570rivate", "/%252570rivate", "/private;v=1", "/private%3Bv=1", "/%2550rivate"):
            rules = rule_sets[1]
            with self.assertRaises(sf.StaticFetchError):
                sf.parse_url("https://good.test" + path)
            verdicts = {rules.allowed("https://good.test" + model) for model in models(path)}
            moved_without_guard += len(verdicts) > 1
        self.assertGreaterEqual(moved_without_guard, 4)

    def test_an_accepted_url_means_the_same_to_robots_as_any_server_resolution_of_it(self):
        import posixpath  # noqa: F401  (documented: resolve() is the same rule as posixpath.normpath minus collapsing a trailing slash)
        import urllib.parse
        from pairadar import static_fetch as sf
        rng = random.Random(SEED + 11)
        tokens = ["a", "b", "/", "/", ".", "..", "%2F", "%2f", "%2e", "%2E", "%5C", "%5c", "%252F", "%252e", "%25", "%2525", "\\"]
        rule_sets = [rr.parse("User-agent: *\nAllow: /a\nDisallow: /b\n", TOKEN), rr.parse("User-agent: *\nDisallow: /a/b\n", TOKEN),
                     rr.parse("User-agent: *\nDisallow: /\nAllow: /a\n", TOKEN), rr.parse("User-agent: *\nAllow: /\nDisallow: /b/a\n", TOKEN)]

        def models(raw: str):
            once = urllib.parse.unquote(raw)
            twice = urllib.parse.unquote(once)
            thrice = urllib.parse.unquote(twice)
            yield raw
            for decoded in (once, twice, thrice):
                yield decoded
                yield decoded.replace("\\", "/")

        accepted = rejected = 0
        for _ in range(6000):
            path = "/" + "".join(rng.choice(tokens) for _ in range(rng.randint(0, 7)))
            try:
                sf.parse_url("https://good.test" + path)
            except sf.StaticFetchError:
                rejected += 1
                continue
            accepted += 1
            for rules in rule_sets:
                want = rules.allowed("https://good.test" + path)
                for model in models(path):
                    seen = self.resolve(model).replace("%", "%25") if "%" in model and not re.search(r"%[0-9A-Fa-f]{2}", model) else self.resolve(model)
                    try:
                        got = rules.allowed("https://good.test" + seen)
                    except rr.RobotsUnsupported:
                        continue                                    # a decoded form that is not valid percent-encoding is not comparable
                    self.assertEqual(got, want, (path, model, seen))
        self.assertGreater(accepted, 300)
        self.assertGreater(rejected, 300)


class NothingIsCutSilently(unittest.TestCase):
    def test_text_is_whole_or_refused_never_in_between(self):
        rng = random.Random(SEED + 6)
        for _ in range(60):
            cap = se.MAX_MARKDOWN_CHARS
            total = cap + rng.choice([-3, -2, -1, 0, 1, 2, 3, 500, -500])
            parts = rng.randint(1, 4)
            sizes = [max(1, (total - (parts - 1)) // parts)] * parts
            sizes[-1] += (total - (parts - 1)) - sum(sizes)                    # the lines plus the newlines between them add up to `total`
            lines = ["w" * size for size in sizes]
            html, expected = page(lines, 0)
            self.assertEqual(expected, total)
            if expected > cap:
                with self.assertRaises(se.ExtractTruncated):
                    se.html_to_markdown(html, "listing")
            else:
                markdown, _ = se.html_to_markdown(html, "listing")
                self.assertEqual(markdown, "\n".join(lines))                  # every character, none cut

    def test_a_listing_keeps_every_link_or_is_refused(self):
        rng = random.Random(SEED + 7)
        for count in sorted({0, 1, 2, se.MAX_ANCHORS - 2, se.MAX_ANCHORS - 1, se.MAX_ANCHORS, se.MAX_ANCHORS + 1, se.MAX_ANCHORS + 2,
                             rng.randint(5, 3000), rng.randint(3001, 3500)}):
            html, _ = page(["Some text for the page."], count)
            if count > se.MAX_ANCHORS:
                with self.assertRaises(se.ExtractTruncated):
                    se.html_to_markdown(html, "listing")
            else:
                markdown, _ = se.html_to_markdown(html, "listing")
                self.assertEqual(markdown.count("]("), count)                  # all of them
                self.assertTrue(all(f"(/news/p{i})" in markdown for i in (0, count - 1)) or count == 0)

    def test_a_page_keeps_the_text_of_every_link_it_cannot_list(self):
        html, _ = page(["Some text for the page."], se.MAX_ANCHORS + 40)
        markdown, _ = se.html_to_markdown(html, "page")
        for i in (0, se.MAX_ANCHORS - 1, se.MAX_ANCHORS, se.MAX_ANCHORS + 39):
            self.assertIn(f"Post {i} title", markdown)

    def test_what_is_counted_is_what_the_mode_keeps(self):
        rng = random.Random(SEED + 8)
        for _ in range(30):
            chrome = rng.choice([0, 10, se.MAX_MARKDOWN_CHARS + 5])
            main = rng.choice([5, se.MAX_MARKDOWN_CHARS - 1, se.MAX_MARKDOWN_CHARS, se.MAX_MARKDOWN_CHARS + 1])
            html = ("<html><body><nav><p>" + "n" * chrome + "</p></nav>" if chrome else "<html><body>") + \
                   "<main><p>" + "m" * main + "</p></main></body></html>"
            keeps_main = main <= se.MAX_MARKDOWN_CHARS
            try:
                markdown, _ = se.html_to_markdown(html, "page")
            except se.ExtractTruncated:
                self.assertFalse(keeps_main)                                    # page mode counts the main text only
            else:
                self.assertTrue(keeps_main)
                self.assertEqual(markdown, "m" * main)
            all_lines = main + (chrome + 1 if chrome else 0)
            try:
                se.html_to_markdown(html, "listing")
            except se.ExtractTruncated:
                self.assertGreater(all_lines, se.MAX_MARKDOWN_CHARS)            # listing mode counts every line
            else:
                self.assertLessEqual(all_lines, se.MAX_MARKDOWN_CHARS)


if __name__ == "__main__":
    unittest.main()
