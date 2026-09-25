from __future__ import annotations

from typing import Any

import pytest

from codeit.jira_client.adf import adf_to_markdown, markdown_to_adf


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def text(t: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": t}
    if marks:
        node["marks"] = list(marks)
    return node


STRONG = {"type": "strong"}
EM = {"type": "em"}
CODE = {"type": "code"}


def link(href: str) -> dict[str, Any]:
    return {"type": "link", "attrs": {"href": href}}


# One case per supported node: canonical markdown and the ADF it must produce.
SUPPORTED: dict[str, tuple[str, dict[str, Any]]] = {
    "paragraph": ("Hello world", doc(para(text("Hello world")))),
    "heading": (
        "## Section",
        doc({"type": "heading", "attrs": {"level": 2}, "content": [text("Section")]}),
    ),
    "bold": ("a **b** c", doc(para(text("a "), text("b", STRONG), text(" c")))),
    "italic": ("a *b* c", doc(para(text("a "), text("b", EM), text(" c")))),
    "bold_italic": ("***x***", doc(para(text("x", STRONG, EM)))),
    "nested_marks": (
        "**a *b***",
        doc(para(text("a ", STRONG), text("b", STRONG, EM))),
    ),
    "inline_code": (
        "run `npm test` now",
        doc(para(text("run "), text("npm test", CODE), text(" now"))),
    ),
    "link": (
        "see [docs](https://example.com/a)",
        doc(para(text("see "), text("docs", link("https://example.com/a")))),
    ),
    "bold_link": (
        "[**docs**](https://e.com)",
        doc(para(text("docs", link("https://e.com"), STRONG))),
    ),
    "code_block": (
        "```python\nprint('hi')\n```",
        doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [text("print('hi')")],
            }
        ),
    ),
    "code_block_no_lang": (
        "```\nx\n```",
        doc({"type": "codeBlock", "attrs": {}, "content": [text("x")]}),
    ),
    "bullet_list": (
        "- one\n- two\n  - nested",
        doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [para(text("one"))]},
                    {
                        "type": "listItem",
                        "content": [
                            para(text("two")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {"type": "listItem", "content": [para(text("nested"))]}
                                ],
                            },
                        ],
                    },
                ],
            }
        ),
    ),
    "ordered_list": (
        "3. a\n4. b",
        doc(
            {
                "type": "orderedList",
                "attrs": {"order": 3},
                "content": [
                    {"type": "listItem", "content": [para(text("a"))]},
                    {"type": "listItem", "content": [para(text("b"))]},
                ],
            }
        ),
    ),
    "blockquote": (
        "> quoted\n>\n> - item",
        doc(
            {
                "type": "blockquote",
                "content": [
                    para(text("quoted")),
                    {
                        "type": "bulletList",
                        "content": [{"type": "listItem", "content": [para(text("item"))]}],
                    },
                ],
            }
        ),
    ),
    "hard_break": ("line\\\nbreak", doc(para(text("line"), {"type": "hardBreak"}, text("break")))),
    "rule": ("---", doc({"type": "rule"})),
}


@pytest.mark.parametrize("name", list(SUPPORTED))
def test_markdown_to_adf(name: str) -> None:
    md, adf = SUPPORTED[name]
    assert markdown_to_adf(md) == adf


@pytest.mark.parametrize("name", list(SUPPORTED))
def test_adf_to_markdown(name: str) -> None:
    md, adf = SUPPORTED[name]
    assert adf_to_markdown(adf) == md


@pytest.mark.parametrize("name", list(SUPPORTED))
def test_round_trip_is_stable(name: str) -> None:
    md, adf = SUPPORTED[name]
    assert markdown_to_adf(adf_to_markdown(adf)) == adf
    assert adf_to_markdown(markdown_to_adf(md)) == md


@pytest.mark.parametrize(
    "literal",
    [
        "2 * 3 * 4",
        "use snake_case and _private",
        "[not a link](x)",
        "a `tick",
        "back\\slash",
        "<b>not html</b>",
        "&amp; stays literal",
        "# not a heading",
        "1. not a list",
        "- not a bullet",
        "> not a quote",
        "---",
        "***",
    ],
)
def test_special_characters_survive(literal: str) -> None:
    adf = doc(para(text(literal)))
    assert markdown_to_adf(adf_to_markdown(adf)) == adf


def test_snake_case_is_not_escaped() -> None:
    assert adf_to_markdown(doc(para(text("my_var_name")))) == "my_var_name"


def test_code_span_with_backticks() -> None:
    adf = doc(para(text("a ` b", CODE)))
    assert adf_to_markdown(adf) == "``a ` b``"
    assert markdown_to_adf(adf_to_markdown(adf)) == adf


def test_code_block_with_fence_inside() -> None:
    adf = doc({"type": "codeBlock", "attrs": {}, "content": [text("```\ninner\n```")]})
    md = adf_to_markdown(adf)
    assert md.startswith("````")
    assert markdown_to_adf(md) == adf


def test_marks_with_surrounding_spaces_stay_valid() -> None:
    adf = doc(para(text("a"), text(" bold ", STRONG), text("b")))
    md = adf_to_markdown(adf)
    assert md == "a **bold** b"


def test_adjacent_mark_changes() -> None:
    adf = doc(para(text("a", STRONG), text("b", STRONG, EM), text("c", EM)))
    assert markdown_to_adf(adf_to_markdown(adf)) == adf


def test_soft_break_becomes_space() -> None:
    assert markdown_to_adf("one\ntwo") == doc(para(text("one two")))


def test_empty_inputs() -> None:
    assert markdown_to_adf("") == doc()
    assert adf_to_markdown(None) == ""
    assert adf_to_markdown(doc()) == ""


def test_heading_inside_list_becomes_bold_paragraph() -> None:
    adf = markdown_to_adf("- # Title")
    item = adf["content"][0]["content"][0]
    assert item["content"] == [para(text("Title", STRONG))]


def test_list_item_starting_with_list_gets_leading_paragraph() -> None:
    adf = markdown_to_adf("- - inner")
    item = adf["content"][0]["content"][0]
    assert item["content"][0] == para()
    assert item["content"][1]["type"] == "bulletList"


def test_nested_blockquote_is_flattened() -> None:
    adf = markdown_to_adf("> > deep")
    assert adf == doc({"type": "blockquote", "content": [para(text("deep"))]})


def test_code_mark_drops_strong() -> None:
    assert markdown_to_adf("**`x`**") == doc(para(text("x", CODE)))


def test_image_degrades_to_link() -> None:
    assert markdown_to_adf("![alt](https://i.png)") == doc(para(text("alt", link("https://i.png"))))


def test_markdown_tables_stay_text() -> None:
    adf = markdown_to_adf("| a | b |\n|---|---|")
    assert adf["content"][0]["type"] == "paragraph"


def test_link_with_spaces_in_href() -> None:
    adf = doc(para(text("x", link("https://e.com/a b"))))
    assert adf_to_markdown(adf) == "[x](<https://e.com/a b>)"


# Unsupported ADF degrades to plain text --------------------------------------------------------


def test_unsupported_inline_nodes_degrade() -> None:
    adf = doc(
        para(
            text("hi "),
            {"type": "mention", "attrs": {"id": "1", "text": "@Onix"}},
            text(" "),
            {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}},
            text(" "),
            {"type": "inlineCard", "attrs": {"url": "https://e.com"}},
            text(" "),
            {"type": "status", "attrs": {"text": "DONE"}},
        )
    )
    assert adf_to_markdown(adf) == "hi @Onix 😄 https://e.com DONE"


def test_mention_without_at_sign() -> None:
    adf = doc(para({"type": "mention", "attrs": {"text": "Onix"}}))
    assert adf_to_markdown(adf) == "@Onix"


def test_panel_renders_its_content() -> None:
    adf = doc({"type": "panel", "attrs": {"panelType": "info"}, "content": [para(text("note"))]})
    assert adf_to_markdown(adf) == "note"


def test_table_degrades_to_rows() -> None:
    def cell(t: str) -> dict[str, Any]:
        return {"type": "tableCell", "content": [para(text(t))]}

    adf = doc(
        {
            "type": "table",
            "content": [
                {"type": "tableRow", "content": [cell("a"), cell("b")]},
                {"type": "tableRow", "content": [cell("1"), cell("2")]},
            ],
        }
    )
    assert adf_to_markdown(adf) == "a | b\n\n1 | 2"


def test_media_is_dropped() -> None:
    adf = doc(para(text("x")), {"type": "mediaSingle", "content": [{"type": "media", "attrs": {}}]})
    assert adf_to_markdown(adf) == "x"


def test_unknown_leaf_node_is_skipped() -> None:
    assert adf_to_markdown(doc({"type": "extension", "attrs": {}})) == ""
