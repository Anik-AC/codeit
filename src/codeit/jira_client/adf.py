"""Markdown <-> Atlassian Document Format conversion (PRD 0.5 and 7.2).

Supported both ways: headings, paragraphs, bold, italic, inline code, code blocks with a
language, bullet lists, ordered lists, links, block quotes, hard breaks and rules.
Anything else degrades to plain text: unsupported ADF nodes render their text, and
markdown outside CommonMark (tables, strikethrough) stays literal text.

Markdown is parsed with markdown-it-py (CommonMark, raw HTML disabled). Soft line breaks
inside a paragraph become spaces, as CommonMark specifies.
"""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

ADFNode = dict[str, Any]

_md = MarkdownIt("commonmark", {"html": False})

# ADF allows only these children inside list items and block quotes.
_NESTED_OK = frozenset({"paragraph", "bulletList", "orderedList", "codeBlock"})


def empty_doc() -> ADFNode:
    return {"type": "doc", "version": 1, "content": []}


# markdown -> ADF ------------------------------------------------------------------------------


def markdown_to_adf(md: str) -> ADFNode:
    """Convert markdown to an ADF document."""
    root = empty_doc()
    stack: list[ADFNode] = [root]
    for tok in _md.parse(md):
        parent = stack[-1]
        if tok.nesting == 1:
            node = _open_block(tok)
            parent.setdefault("content", []).append(node)
            stack.append(node)
        elif tok.nesting == -1:
            stack.pop()
        elif tok.type == "inline":
            parent.setdefault("content", []).extend(_inline_to_adf(tok.children or []))
        elif tok.type in ("fence", "code_block"):
            parent.setdefault("content", []).append(_code_block(tok))
        elif tok.type == "hr":
            parent.setdefault("content", []).append({"type": "rule"})
    root["content"] = _sanitize_children(root["content"], "doc")
    return root


def _open_block(tok: Token) -> ADFNode:
    match tok.type:
        case "heading_open":
            return {"type": "heading", "attrs": {"level": int(tok.tag[1])}, "content": []}
        case "bullet_list_open":
            return {"type": "bulletList", "content": []}
        case "ordered_list_open":
            start = tok.attrGet("start")
            return {"type": "orderedList", "attrs": {"order": int(start or 1)}, "content": []}
        case "list_item_open":
            return {"type": "listItem", "content": []}
        case "blockquote_open":
            return {"type": "blockquote", "content": []}
        case _:
            return {"type": "paragraph", "content": []}


def _code_block(tok: Token) -> ADFNode:
    node: ADFNode = {"type": "codeBlock", "attrs": {}}
    lang = tok.info.strip().split()[0] if tok.info.strip() else ""
    if lang:
        node["attrs"]["language"] = lang
    text = tok.content.removesuffix("\n")
    if text:
        node["content"] = [{"type": "text", "text": text}]
    return node


def _marks(strong: int, em: int, links: list[str], code: bool = False) -> list[ADFNode]:
    """Marks in canonical order: link, then strong/em, or code (which excludes strong/em)."""
    marks: list[ADFNode] = []
    if links:
        marks.append({"type": "link", "attrs": {"href": links[-1]}})
    if code:
        marks.append({"type": "code"})
        return marks
    if strong:
        marks.append({"type": "strong"})
    if em:
        marks.append({"type": "em"})
    return marks


def _push_text(out: list[ADFNode], text: str, marks: list[ADFNode]) -> None:
    if not text:
        return
    prev = out[-1] if out else None
    if prev is not None and prev["type"] == "text" and prev.get("marks", []) == marks:
        prev["text"] += text
        return
    node: ADFNode = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    out.append(node)


def _inline_to_adf(children: list[Token]) -> list[ADFNode]:
    out: list[ADFNode] = []
    strong = em = 0
    links: list[str] = []
    for tok in children:
        match tok.type:
            case "text":
                _push_text(out, tok.content, _marks(strong, em, links))
            case "softbreak":
                _push_text(out, " ", _marks(strong, em, links))
            case "hardbreak":
                out.append({"type": "hardBreak"})
            case "code_inline":
                _push_text(out, tok.content, _marks(strong, em, links, code=True))
            case "strong_open":
                strong += 1
            case "strong_close":
                strong -= 1
            case "em_open":
                em += 1
            case "em_close":
                em -= 1
            case "link_open":
                links.append(str(tok.attrGet("href") or ""))
            case "link_close":
                links.pop()
            case "image":
                alt = tok.content or str(tok.attrGet("src") or "")
                src = str(tok.attrGet("src") or "")
                _push_text(out, alt, _marks(strong, em, [*links, src] if src else links))
            case _:
                _push_text(out, tok.content, _marks(strong, em, links))
    return out


def _sanitize_children(nodes: list[ADFNode], parent: str) -> list[ADFNode]:
    """Rewrite structures ADF rejects: headings, quotes and rules inside lists or quotes,
    and list items that do not start with a paragraph or code block."""
    nested = parent in ("listItem", "blockquote")
    out: list[ADFNode] = []
    for node in nodes:
        kind = node["type"]
        if "content" in node and kind not in ("paragraph", "heading", "codeBlock"):
            node["content"] = _sanitize_children(node["content"], kind)
        if nested and kind not in _NESTED_OK:
            if kind == "heading":
                out.append(_heading_as_paragraph(node))
            elif kind == "blockquote":
                out.extend(node.get("content", []))
            # a rule has no valid place here and carries no text: drop it
            continue
        out.append(node)
    if parent == "listItem" and (not out or out[0]["type"] not in ("paragraph", "codeBlock")):
        out.insert(0, {"type": "paragraph", "content": []})
    return out


def _heading_as_paragraph(node: ADFNode) -> ADFNode:
    content = []
    for child in node.get("content", []):
        marks = child.get("marks", [])
        if child["type"] == "text" and not any(m["type"] in ("code", "strong") for m in marks):
            links = [m for m in marks if m["type"] == "link"]
            others = [m for m in marks if m["type"] != "link"]
            child = {**child, "marks": [*links, {"type": "strong"}, *others]}
        content.append(child)
    return {"type": "paragraph", "content": content}


# ADF -> markdown ------------------------------------------------------------------------------


def adf_to_markdown(doc: ADFNode | None) -> str:
    """Convert an ADF document (or `None`) to markdown."""
    if not doc:
        return ""
    return _blocks(doc.get("content", []))


def _blocks(nodes: list[ADFNode]) -> str:
    parts = [_block(n) for n in nodes]
    return "\n\n".join(p for p in parts if p)


def _block(node: ADFNode) -> str:
    kind = node.get("type")
    content: list[ADFNode] = node.get("content", [])
    attrs: dict[str, Any] = node.get("attrs") or {}
    match kind:
        case "paragraph":
            return _escape_line_starts(_inline_md(content))
        case "heading":
            level = min(6, max(1, int(attrs.get("level", 1))))
            return "#" * level + " " + _inline_md(content)
        case "codeBlock":
            return _fence("".join(_plain(c) for c in content), attrs.get("language") or "")
        case "bulletList":
            return "\n".join(_list_item(item, "- ") for item in content)
        case "orderedList":
            start = int(attrs.get("order", 1))
            return "\n".join(_list_item(item, f"{start + i}. ") for i, item in enumerate(content))
        case "blockquote":
            inner = _blocks(content)
            return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
        case "rule":
            return "---"
        case "table":
            return "\n\n".join(
                _escape_line_starts(
                    _escape(" | ".join(_plain(cell) for cell in row.get("content", [])))
                )
                for row in content
            )
        case "media" | "mediaSingle" | "mediaGroup" | "mediaInline":
            return ""
        case _:
            if any(c.get("type") in _INLINE_TYPES for c in content):
                return _escape_line_starts(_inline_md(content))
            if content:
                return _blocks(content)
            text = _inline_fallback(node)
            return _escape(text) if text else ""


def _list_item(item: ADFNode, marker: str) -> str:
    indent = " " * len(marker)
    children: list[ADFNode] = item.get("content", [])
    out = ""
    for i, child in enumerate(children):
        rendered = _block(child)
        if i == 0:
            out = rendered
            continue
        sep = "\n" if child.get("type") in ("bulletList", "orderedList") else "\n\n"
        out += sep + rendered
    lines = out.split("\n")
    body = "\n".join([lines[0], *[(indent + ln) if ln else "" for ln in lines[1:]]])
    return (marker + body).rstrip()


def _fence(code: str, lang: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{code}\n{fence}"


_INLINE_TYPES = frozenset(
    {"text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status", "placeholder"}
)
# Emphasis delimiters. `_` variants are used right after a `*` run, where another `*` run
# would merge with it and change the parse (e.g. `**a*b***` followed by `*c*`).
_DELIMS = {"strong": ("**", "__"), "em": ("*", "_")}

Mark = tuple[str, str]  # (kind, href) where href is "" unless kind == "link"


def _wanted_marks(marks: list[ADFNode]) -> tuple[list[Mark], bool]:
    link = [
        ("link", str((m.get("attrs") or {}).get("href", "")))
        for m in marks
        if m.get("type") == "link"
    ]
    code = any(m.get("type") == "code" for m in marks)
    rest = [
        (k, "") for k in ("strong", "em") if not code and any(m.get("type") == k for m in marks)
    ]
    return link[:1] + rest, code


def _inline_md(nodes: list[ADFNode]) -> str:
    out: list[str] = []
    open_: list[Mark] = []
    closers: list[str] = []

    def close_to(depth: int) -> None:
        trailing = _pop_trailing_spaces(out)
        while len(open_) > depth:
            open_.pop()
            out.append(closers.pop())
        out.append(trailing)

    def last_char() -> str:
        return next((chunk[-1] for chunk in reversed(out) if chunk), "")

    for node in nodes:
        kind = node.get("type")
        if kind == "hardBreak":
            out.append("\\\n")
            continue
        if kind == "text":
            text = str(node.get("text", ""))
            want, code = _wanted_marks(node.get("marks") or [])
        else:
            text, want, code = _inline_fallback(node), list(open_), False
        if not text:
            continue
        keep = 0
        while keep < min(len(open_), len(want)) and open_[keep] == want[keep]:
            keep += 1
        if keep < len(open_):
            close_to(keep)
        if len(want) > keep:
            stripped = text.lstrip(" ")
            out.append(text[: len(text) - len(stripped)])
            text = stripped
            if not text:
                continue
            after_star = last_char() == "*"
            for i, mark in enumerate(want[keep:]):
                kind_, href = mark
                if kind_ == "link":
                    opener, closer = "[", f"]({_href(href)})"
                else:
                    opener = _DELIMS[kind_][1 if after_star and i == 0 else 0]
                    closer = opener
                out.append(opener)
                open_.append(mark)
                closers.append(closer)
        out.append(_code_span(text) if code else _escape(text))
    close_to(0)
    return "".join(out)


def _pop_trailing_spaces(out: list[str]) -> str:
    moved = ""
    while out:
        last = out[-1]
        stripped = last.rstrip(" ")
        moved = last[len(stripped) :] + moved
        if stripped:
            out[-1] = stripped
            break
        out.pop()
    return moved


def _inline_fallback(node: ADFNode) -> str:
    """Plain text for inline nodes markdown cannot express."""
    attrs: dict[str, Any] = node.get("attrs") or {}
    match node.get("type"):
        case "mention":
            text = str(attrs.get("text") or "")
            return text if text.startswith("@") or not text else f"@{text}"
        case "emoji":
            return str(attrs.get("text") or attrs.get("shortName") or "")
        case "inlineCard" | "blockCard" | "embedCard":
            return str(attrs.get("url") or "")
        case "date":
            return str(attrs.get("timestamp") or "")
        case "status":
            return str(attrs.get("text") or "")
        case _:
            return _plain(node)


def _plain(node: ADFNode) -> str:
    if node.get("type") == "text":
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    children = node.get("content") or []
    if not children:
        return _inline_fallback(node) if node.get("type") in _INLINE_TYPES else ""
    return "".join(_plain(c) for c in children)


def _code_span(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = (
        text.startswith("`")
        or text.endswith("`")
        or (text.startswith(" ") and text.endswith(" ") and text.strip(" ") != "")
    )
    return f"{fence} {text} {fence}" if pad else f"{fence}{text}{fence}"


def _href(href: str) -> str:
    if re.search(r"[\s()<>]", href):
        return "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
    return href


_ESCAPE_CHARS = re.compile(r"([\\`*\[\]<])")
_ESCAPE_UNDERSCORE = re.compile(r"(?<!\w)_|_(?!\w)")
_ESCAPE_ENTITY = re.compile(r"&(?=#?[A-Za-z0-9]+;)")
_LINE_START = re.compile(
    r"^(?P<lead> {0,3})(?:(?P<hash>#{1,6})(?=\s|$)|(?P<gt>>)|(?P<bullet>[-+])(?=\s|$)"
    r"|(?P<num>\d{1,9})(?P<dot>[.)])(?=\s|$)|(?P<rule>(?:[-=~] *){3,}$))"
)


def _escape(text: str) -> str:
    text = _ESCAPE_CHARS.sub(r"\\\1", text)
    text = _ESCAPE_UNDERSCORE.sub(r"\\_", text)
    return _ESCAPE_ENTITY.sub(r"\\&", text)


def _escape_line_starts(md: str) -> str:
    """Escape text at the start of a paragraph line that would parse as a block marker."""
    return "\n".join(_escape_line_start(line) for line in md.split("\n"))


def _escape_line_start(line: str) -> str:
    m = _LINE_START.match(line)
    if not m:
        return line
    lead = m.group("lead")
    rest = line[len(lead) :]
    if m.group("num"):
        n = len(m.group("num"))
        return lead + rest[:n] + "\\" + rest[n:]
    return lead + "\\" + rest
