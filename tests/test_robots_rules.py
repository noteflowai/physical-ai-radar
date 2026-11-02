"""robots_rules: RFC 9309 matching for the radar's product token (no network)."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import robots_rules as rr  # noqa: E402

TOKEN = "physical-ai-radar"
BASE = "https://good.test"


def allowed(text: str, path: str) -> bool:
    return rr.parse(text, TOKEN).allowed(BASE + path)


class RuleTests(unittest.TestCase):
    def test_the_longest_match_wins_whatever_the_order(self):
        for order in ("Allow: /\nDisallow: /private\n", "Disallow: /private\nAllow: /\n"):
            text = "User-agent: *\n" + order
            self.assertFalse(allowed(text, "/private"), order)           # Python's robotparser says allowed for the first
            self.assertFalse(allowed(text, "/private/x"), order)
            self.assertTrue(allowed(text, "/public"), order)
        text = "User-agent: *\nDisallow: /a\nAllow: /a/b\n"
        self.assertTrue(allowed(text, "/a/b/c"))
        self.assertFalse(allowed(text, "/a/c"))
        self.assertFalse(allowed(text, "/a"))

    def test_a_tie_goes_to_allow_and_no_match_allows(self):
        self.assertTrue(allowed("User-agent: *\nDisallow: /x\nAllow: /x\n", "/x"))
        self.assertTrue(allowed("User-agent: *\nAllow: /x\nDisallow: /x\n", "/x"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /x\n", "/y"))
        self.assertTrue(allowed("", "/anything"))
        self.assertTrue(allowed("User-agent: *\n", "/anything"))

    def test_empty_rules_are_no_rules(self):
        self.assertTrue(allowed("User-agent: *\nDisallow:\n", "/x"))
        self.assertTrue(allowed("User-agent: *\nDisallow:   # nothing\n", "/x"))
        self.assertFalse(allowed("User-agent: *\nAllow:\nDisallow: /\n", "/x"))

    def test_wildcards_and_the_end_anchor(self):
        text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /*/private\nDisallow: /tmp*\n"
        self.assertFalse(allowed(text, "/a/b.pdf"))
        self.assertTrue(allowed(text, "/a/b.pdf?x=1"))                  # `$` ends the path, the query is part of it
        self.assertTrue(allowed(text, "/a/b.pdfx"))
        self.assertFalse(allowed(text, "/x/private/y"))
        self.assertFalse(allowed(text, "/tmp"))
        self.assertFalse(allowed(text, "/tmpfile"))
        self.assertTrue(allowed(text, "/other"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /a$\n", "/a"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /a$\n", "/ab"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /a$\n", "/a/"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /a*b$\n", "/axxb"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /a*b$\n", "/axxbc"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /ab*b$\n", "/abb"))          # the end part may not overlap the start
        self.assertTrue(allowed("User-agent: *\nDisallow: /abb*b$\n", "/abb"))

    def test_the_query_is_part_of_the_path_that_is_matched(self):
        self.assertFalse(allowed("User-agent: *\nDisallow: /search?\n", "/search?q=1"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /search?\n", "/search"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /*?session=\n", "/a?session=1"))

    def test_percent_encoding_is_compared_in_one_form(self):
        self.assertFalse(allowed("User-agent: *\nDisallow: /caf%C3%A9\n", "/caf%c3%a9"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /café\n", "/caf%C3%A9"))              # a non-ASCII rule is its UTF-8 octets
        self.assertFalse(allowed("User-agent: *\nDisallow: /%7Euser\n", "/~user"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /~user\n", "/%7euser"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /%61bc\n", "/abc"))                  # an unreserved letter, decoded
        self.assertTrue(allowed("User-agent: *\nDisallow: /a%2Fb\n", "/a/b"))                   # an encoded slash is not a slash
        self.assertFalse(allowed("User-agent: *\nDisallow: /a%2Fb\n", "/a%2fb"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /a%2Ab\n", "/axb"))                   # an encoded asterisk is no wildcard
        self.assertFalse(allowed("User-agent: *\nDisallow: /a%2Ab\n", "/a%2ab"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /100%\n", "/100%"))                  # a stray percent sign is literal

    def test_encoded_special_characters_are_data_not_syntax(self):
        """RFC 9309 Figure 6: `%2A` and `%24` in a rule are the characters `*` and `$` in the URL (01's R-D-4b12-01)."""
        for rule, path, expected in (
                ("/foo%2A", "/foo*", False), ("/foo%2a", "/foo*", False), ("/foo%24", "/foo$", False),
                ("/path/file-with-a-%2A.html", "/path/file-with-a-*.html", False), ("/path/foo-%24", "/path/foo-$", False),
                ("/foo%2A", "/foo%2A", False), ("/foo%24", "/foo%24", False), ("/foo%2A", "/foo%2a", False),
                ("/foo%2A", "/fooX", True),                          # an encoded asterisk matches an asterisk, not anything
                ("/foo%2A", "/foo", True), ("/a%2Ab", "/axb", True), ("/a%2Ab", "/a*b", False), ("/a%24b", "/axb", True),
                ("/a*b", "/axb", False), ("/a*b", "/a%2Ab", False), ("/a*b", "/a*b", False),           # a literal wildcard matches both forms
                ("/a$b", "/a$b", False), ("/a$b", "/ab", True),                                       # `$` inside a rule is data
                ("/a%24", "/a", True), ("/foo%24$", "/foo$", False), ("/foo%24$", "/foo$x", True),    # `$` ends the rule; `%24` is data
                ("/foo*%2A", "/fooabc*", False), ("/foo*%2A", "/fooabc", True)):
            with self.subTest(rule=rule, path=path):
                self.assertEqual(allowed(f"User-agent: *\nDisallow: {rule}\n", path), expected)
        # with Allow, the same equivalence decides the longer rule
        self.assertTrue(allowed("User-agent: *\nDisallow: /\nAllow: /foo%2A\n", "/foo*"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /\nAllow: /foo%2A\n", "/foox"))

    def test_a_search_never_starts_in_the_middle_of_an_escape(self):
        # the letter B is inside the escape %2B, but the URL contains the octet "+", not the letter
        self.assertTrue(allowed("User-agent: *\nDisallow: /*B\n", "/%2B"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /*2B\n", "/%2B"))
        self.assertTrue(allowed("User-agent: *\nDisallow: /x*A\n", "/x%2A"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /*%2B\n", "/%2B"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /*B\n", "/%42"))                      # %42 is the letter B
        self.assertFalse(allowed("User-agent: *\nDisallow: /a*%2B$\n", "/ab%2b"))

    def test_a_stray_percent_sign_is_data_and_never_becomes_an_escape(self):
        self.assertEqual(rr.normalise("%4%31"), "%2541")                    # the data `%`, `4`, `1`; not the letter A
        self.assertEqual(rr.normalise(rr.normalise("%4%31")), "%2541")
        self.assertEqual(rr.normalise("%"), "%25")
        self.assertEqual(rr.normalise("%zz"), "%25zz")
        self.assertEqual(rr.normalise("%4"), "%254")
        self.assertEqual(rr.normalise("%25"), "%25")
        self.assertFalse(allowed("User-agent: *\nDisallow: /%4%31\n", "/%4%31"))       # the same data on both sides
        self.assertTrue(allowed("User-agent: *\nDisallow: /%4%31\n", "/A"))             # not a spelling of A
        self.assertTrue(allowed("User-agent: *\nDisallow: /A\n", "/%4%31"))
        self.assertFalse(allowed("User-agent: *\nDisallow: /%41\n", "/A"))              # a real escape of a letter is the letter

    def test_which_group_applies(self):
        # our token beats `*`
        self.assertTrue(allowed("User-agent: *\nDisallow: /\n\nUser-agent: physical-ai-radar\nAllow: /\n", "/x"))
        self.assertFalse(allowed("User-agent: physical-ai-radar\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "/x"))
        # token matching ignores case, and a version suffix is the same product
        self.assertFalse(allowed("User-agent: Physical-AI-Radar\nDisallow: /x\n", "/x"))
        self.assertFalse(allowed("User-agent: physical-ai-radar/1.0\nDisallow: /x\n", "/x"))
        # consecutive User-agent lines share the rules that follow
        self.assertFalse(allowed("User-agent: other\nUser-agent: physical-ai-radar\nDisallow: /x\n", "/x"))
        # a User-agent line after rules starts a new group
        self.assertTrue(allowed("User-agent: other\nDisallow: /x\nUser-agent: physical-ai-radar\nDisallow: /y\n", "/x"))
        # several groups naming us, or several `*` groups, are merged
        text = "User-agent: physical-ai-radar\nDisallow: /a\n\nUser-agent: other\nDisallow: /z\n\nUser-agent: physical-ai-radar\nDisallow: /b\n"
        self.assertFalse(allowed(text, "/a"))
        self.assertFalse(allowed(text, "/b"))
        self.assertTrue(allowed(text, "/z"))
        text = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"
        self.assertFalse(allowed(text, "/a"))
        self.assertFalse(allowed(text, "/b"))
        # only other agents, or a token that merely contains ours: everything is allowed
        self.assertTrue(allowed("User-agent: googlebot\nDisallow: /\n", "/x"))
        self.assertTrue(allowed("User-agent: not-physical-ai-radar\nDisallow: /\n", "/x"))
        self.assertTrue(allowed("User-agent: physical-ai-radar-extra\nDisallow: /\n", "/x"))

    def test_the_shape_of_the_file_does_not_matter(self):
        text = "\ufeffUser-agent: *\r\nCrawl-delay: 10\r\nSitemap: https://good.test/s.xml\r\nDisallow: /x # why\r\n"
        self.assertFalse(allowed(text, "/x"))                            # BOM, CRLF, a comment, directives that are ignored
        self.assertFalse(allowed("User-agent: *\rDisallow: /x\r", "/x"))     # CR alone ends a line
        self.assertFalse(allowed("USER-AGENT: *\nDISALLOW: /x\n", "/x"))
        self.assertFalse(allowed("User-agent : *\nDisallow : /x\n", "/x"))
        self.assertTrue(allowed("Disallow: /x\nUser-agent: *\nAllow: /\n", "/x"))     # rules before any User-agent line are ignored
        self.assertTrue(allowed("User-agent: *\nDisalow: /x\n", "/x"))                # a misspelt directive is nothing
        self.assertTrue(allowed("User-agent: *\nthis is not a directive\n", "/x"))

    def test_a_file_that_cannot_be_judged_completely_is_not_guessed_at(self):
        with self.assertRaises(rr.RobotsUnsupported):
            rr.parse("User-agent: *\n" + "".join(f"Disallow: /p{i}\n" for i in range(rr.MAX_RULES + 1)), TOKEN)
        rr.parse("User-agent: *\n" + "".join(f"Disallow: /p{i}\n" for i in range(rr.MAX_RULES)), TOKEN)         # exactly the limit is fine
        with self.assertRaises(rr.RobotsUnsupported):
            rr.parse("User-agent: *\nDisallow: /" + "a" * rr.MAX_PATTERN + "\n", TOKEN)
        rr.parse("User-agent: *\nDisallow: /" + "a" * (rr.MAX_PATTERN - 1) + "\n", TOKEN)

    def test_pathological_patterns_stay_fast(self):
        text = "User-agent: *\nDisallow: /" + "*a" * 900 + "*b\n"
        started = time.monotonic()
        self.assertTrue(allowed(text, "/" + "a" * 400))
        self.assertLess(time.monotonic() - started, 2)

    def test_a_rule_can_be_the_last_thing_in_a_long_file(self):
        text = "#" + "c" * 499_999 + "\nUser-agent: *\nDisallow: /\n"
        self.assertFalse(allowed(text, "/news"))


if __name__ == "__main__":
    unittest.main()
