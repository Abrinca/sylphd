"""web/markdown.js renders operator text from /info: it must be correct and, above all, inert.

The renderer only needs createElement / createTextNode / createDocumentFragment, so the tests run it
in node against a tiny fake DOM and inspect the resulting tree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[1] / "web" / "markdown.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

NODE = r"""
const md = require(process.argv[1]);
class Node_ { constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { this.children.push(c); return c; } }
const doc = {
  createElement: (t) => new Node_(t),
  createTextNode: (s) => ({ text: String(s) }),
  createDocumentFragment: () => new Node_('#fragment'),
};
const dump = (n) => (n.text !== undefined ? n.text : { tag: n.tag, attrs: n.attrs, children: n.children.map(dump) });
const inputs = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = inputs.map((src) => {
  const t0 = process.hrtime.bigint();
  const tree = dump(md.render(src, doc));
  return { tree, ms: Number(process.hrtime.bigint() - t0) / 1e6 };
});
const urls = JSON.parse(process.argv[2]).map((u) => md.safeUrl(u));
console.log(JSON.stringify({ out, urls }));
"""

ALLOWED_TAGS = {"#fragment", "p", "br", "strong", "em", "code", "a", "ul", "ol", "li",
                "h3", "h4", "h5", "blockquote"}


def run(inputs: list[str], urls: list[str] | None = None) -> tuple[list[dict], list]:
    done = subprocess.run(
        ["node", "-e", NODE, str(WEB_JS), json.dumps(urls or [])],
        input=json.dumps(inputs), capture_output=True, text=True, check=True,
    )
    data = json.loads(done.stdout)
    return data["out"], data["urls"]


def html(node) -> str:
    """Compact serialization for assertions: text is NOT escaped (it is a text node in the DOM)."""
    if isinstance(node, str):
        return node
    inner = "".join(html(c) for c in node["children"])
    if node["tag"] == "#fragment":
        return inner
    attrs = "".join(f' {k}="{v}"' for k, v in node["attrs"].items())
    if node["tag"] == "br":
        return "<br>"
    return f"<{node['tag']}{attrs}>{inner}</{node['tag']}>"


def one(src: str) -> str:
    out, _ = run([src])
    return html(out[0]["tree"])


def walk(node):
    if isinstance(node, str):
        return
    yield node
    for child in node["children"]:
        yield from walk(child)


def assert_inert(tree) -> None:
    """Only whitelisted tags; only href/target/rel/start attributes; every href safe."""
    for node in walk(tree):
        assert node["tag"] in ALLOWED_TAGS, node["tag"]
        for name, value in node["attrs"].items():
            assert name in {"href", "target", "rel", "start"}, name
            if name == "href":
                assert value.split(":", 1)[0] in {"http", "https", "mailto"}, value
        if node["tag"] == "a":
            assert node["attrs"]["target"] == "_blank"
            assert node["attrs"]["rel"] == "noopener noreferrer nofollow ugc"


EXAMPLE = (
    "This sylphd server contains the [official pre-built]"
    "(https://sylph-docs.github.io/pre%E2%80%90built-databases/) gtdb-r232-c200-dbv2.syl2db database. "
    "It contains 199,923 prokaryotic species from GTDB r232, but no eukaryotes (fungi, human, etc.)."
)


def test_operator_example() -> None:
    assert one(EXAMPLE) == (
        '<p>This sylphd server contains the <a href="https://sylph-docs.github.io/pre%E2%80%90built-databases/" '
        'target="_blank" rel="noopener noreferrer nofollow ugc">official pre-built</a> gtdb-r232-c200-dbv2.syl2db '
        "database. It contains 199,923 prokaryotic species from GTDB r232, but no eukaryotes (fungi, human, etc.).</p>"
    )


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("plain", "<p>plain</p>"),
        ("one\ntwo", "<p>one<br>two</p>"),
        ("a\n\nb", "<p>a</p><p>b</p>"),
        ("**bold** and *it* and _it2_", "<p><strong>bold</strong> and <em>it</em> and <em>it2</em></p>"),
        ("***both***", "<p><strong><em>both</em></strong></p>"),
        ("`code *not em*`", "<p><code>code *not em*</code></p>"),
        ("``a ` b``", "<p><code>a ` b</code></p>"),
        ("snake_case_name stays", "<p>snake_case_name stays</p>"),
        ("2 * 3 * 4", "<p>2 * 3 * 4</p>"),
        ("# Title\ntext", "<h3>Title</h3><p>text</p>"),
        ("### Sub", "<h4>Sub</h4>"),
        ("###### Deep", "<h5>Deep</h5>"),
        ("- a\n- b **x**\n  more", "<ul><li>a</li><li>b <strong>x</strong><br>more</li></ul>"),
        ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
        ("3. a\n4. b", '<ol start="3"><li>a</li><li>b</li></ol>'),
        ("> quoted\n> text", "<blockquote><p>quoted<br>text</p></blockquote>"),
        ("para\n- item", "<p>para</p><ul><li>item</li></ul>"),
        ("---\ntext", "<p>text</p>"),
        (r"\*literal\* \[x\]", "<p>*literal* [x]</p>"),
        ("see https://example.org/a_b.", '<p>see <a href="https://example.org/a_b" target="_blank" rel="noopener noreferrer nofollow ugc">https://example.org/a_b</a>.</p>'),
        ("(https://example.org/x)", '<p>(<a href="https://example.org/x" target="_blank" rel="noopener noreferrer nofollow ugc">https://example.org/x</a>)</p>'),
        ("[**b** link](mailto:a@b.org)", '<p><a href="mailto:a@b.org" target="_blank" rel="noopener noreferrer nofollow ugc"><strong>b</strong> link</a></p>'),
        ("[wiki](https://en.wikipedia.org/wiki/A_(b))", '<p><a href="https://en.wikipedia.org/wiki/A_(b)" target="_blank" rel="noopener noreferrer nofollow ugc">wiki</a></p>'),
        ("Δ genomes — ✓", "<p>Δ genomes — ✓</p>"),
        ("", ""),
        ("   \n\n  ", ""),
    ],
)
def test_rendering(src: str, expected: str) -> None:
    assert one(src) == expected


def test_html_is_text() -> None:
    out = one('<b>bold</b> <script>alert(1)</script> <img src=x onerror=alert(1)>')
    assert out == '<p><b>bold</b> <script>alert(1)</script> <img src=x onerror=alert(1)></p>'
    # ...which, being a text node, is never parsed: the tree has no script or img element
    tree, _ = run(['<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>'])
    assert_inert(tree[0]["tree"])
    assert not [n for n in walk(tree[0]["tree"]) if n["tag"] in {"script", "img"}]


@pytest.mark.parametrize(
    "payload",
    [
        "[x](javascript:alert(1))",
        "[x](JaVaScRiPt:alert(1))",
        "[x](  javascript:alert(1))",
        "[x](java\tscript:alert(1))",
        "[x](java&#x09;script:alert(1))",
        "[x](&#106;avascript:alert(1))",
        "[x](data:text/html,<script>alert(1)</script>)",
        "[x](DATA:text/html;base64,PHNjcmlwdD4=)",
        "[x](vbscript:msgbox(1))",
        "[x](//evil.example/x)",
        "[x](\\\\evil.example\\x)",
        "[x](/relative/path)",
        "[x](#fragment)",
        "[x](file:///etc/passwd)",
        "[x](ftp://example.org/x)",
        "[x](blob:https://example.org/uuid)",
        "[x](https://)",
        "[x](http://exa mple.org)",
        "![img](https://example.org/a.png)",
        "![x](javascript:alert(1))",
        "<a href=\"javascript:alert(1)\">x</a>",
        "<https://example.org>",
        "[x]( <javascript:alert(1)> )",
        "[a](b)(javascript:alert(1))",
        "[[x](javascript:alert(1))](https://ok.example)",
        "javascript:alert(1)",
        "[x]: javascript:alert(1)\n\n[x]",
    ],
)
def test_unsafe_payloads_are_inert(payload: str) -> None:
    out, _ = run([payload])
    tree = out[0]["tree"]
    assert_inert(tree)
    hrefs = [n["attrs"]["href"] for n in walk(tree) if n["tag"] == "a"]
    assert all(h.startswith(("https://", "http://", "mailto:")) for h in hrefs)
    # a payload that is not a clean https link never yields a link to anything but the outer safe one
    assert not [h for h in hrefs if "javascript" in h.lower() or h.startswith(("data:", "vbscript:"))]


def test_unsafe_link_shows_its_source_as_text() -> None:
    assert one("[x](javascript:alert(1))") == "<p>[x](javascript:alert(1))</p>"
    assert one("![alt](https://example.org/a.png)") == "<p>![alt](https://example.org/a.png)</p>"


def test_safe_url() -> None:
    cases = ["https://example.org/a?b=c#d", "HTTP://EXAMPLE.ORG", "mailto:me@example.org",
             "javascript:alert(1)", "//evil", "/x", "https:", "https://", "http://a b", " https://ok.example ",
             "https://ex\u2028ample.org", "mailto:", "https://example.org/\u0000x", "ftp://x", ""]
    _, urls = run([], cases)
    assert urls == [
        "https://example.org/a?b=c#d", "http://example.org/", "mailto:me@example.org",
        None, None, None, None, None, None, "https://ok.example/", None, None, None, None, None,
    ]


@pytest.mark.parametrize(
    "src",
    [
        "**unbalanced", "*a **b* c**", "_a *b_ c*", "[unclosed", "[a](b", "[a](", "]]]][[[[", "(((((",
        "`unclosed", "``` `` ` ``", "* * * *", "** **", "[](", "![", "\\", "[x](<", "# ", "> > > > > > > deep",
        "- - - - -", "1. 2. 3.", "<<<>>>", "\u0000\u200b\ufeff",
    ],
)
def test_malformed_input_does_not_break(src: str) -> None:
    out, _ = run([src])
    assert_inert(out[0]["tree"])


def test_nesting_is_bounded() -> None:
    out, _ = run(["*" * 5 + "x" + "*" * 5, "> " * 50 + "deep", "**a *b **c *d **e *f g* h** i* j** k* l**"])
    for item in out:
        assert_inert(item["tree"])

    def depth(node) -> int:
        return 0 if isinstance(node, str) else 1 + max([depth(c) for c in node["children"]] or [0])

    assert depth(out[1]["tree"]) <= 8
    assert depth(out[2]["tree"]) <= 10


@pytest.mark.parametrize(
    "src",
    [
        "a" * 200_000,
        "[" * 100_000,
        "[a](" * 20_000,
        "*a " * 50_000,
        "_a " * 50_000,
        "`" * 100_000,
        "`a`` " * 20_000,
        "**" * 60_000,
        "[x](https://example.org) " * 20_000,
        "http://" * 20_000,
        "> " * 100_000,
        "- " * 100_000,
        "\\" * 100_000,
        ("*a* " * 5_000) + "*",
        "[" * 5000 + "x" + "](https://example.org)" * 5000,
        "\n" * 100_000,
        "a\n" * 50_000,
    ],
)
def test_pathological_input_is_fast_and_bounded(src: str) -> None:
    out, _ = run([src])
    assert out[0]["ms"] < 1500, out[0]["ms"]
    assert_inert(out[0]["tree"])
    assert len(json.dumps(out[0]["tree"])) < 2_000_000


def test_long_input_is_cut() -> None:
    out, _ = run(["word " * 10_000])
    text = html(out[0]["tree"])
    assert text.endswith("<p>…</p>")
    assert len(text) < 17_000
