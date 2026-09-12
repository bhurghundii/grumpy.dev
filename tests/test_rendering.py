"""app/rendering.py's render_markdown: pure function, no DB involved —
deliberately doesn't pull in the Testcontainers-backed grumpy_env fixture
chain, same reasoning as tests/test_config.py.

The sanitization cases matter more than the conversion ones: this text
ultimately traces back to a PR diff, an attacker-controlled input to the
grading model, and is shown on an unauthenticated, token-in-URL page (see
app/rendering.py's docstring) — a regression here is a live XSS vector,
not just a formatting nit.
"""

from __future__ import annotations

from markupsafe import Markup

from app.rendering import render_markdown


def test_bold_renders_as_strong() -> None:
    assert "<strong>bold</strong>" in render_markdown("this is **bold** text")


def test_bullet_list_renders_as_list_items() -> None:
    html = render_markdown("- first\n- second\n")
    assert "<ul>" in html
    assert "<li>first</li>" in html
    assert "<li>second</li>" in html


def test_fenced_code_block_renders_as_pre_code() -> None:
    html = render_markdown("```\nsome_code()\n```")
    assert "<pre>" in html
    assert "<code>" in html
    assert "some_code()" in html


def test_returns_markup_instance_so_jinja2_does_not_reescape() -> None:
    assert isinstance(render_markdown("**bold**"), Markup)


def test_none_input_renders_to_empty_markup() -> None:
    assert render_markdown(None) == Markup("")


def test_empty_string_renders_to_empty_markup() -> None:
    assert render_markdown("") == Markup("")


def test_script_tag_is_stripped() -> None:
    html = render_markdown("normal text\n\n<script>alert(1)</script>\n\nmore text")
    assert "<script" not in html


def test_script_tag_embedded_in_a_list_item_is_stripped() -> None:
    # Markdown passes raw HTML blocks through untouched by default — the
    # sanitizer, not the markdown converter, is what has to catch this.
    html = render_markdown("- claim one\n- <script>alert(document.cookie)</script>\n")
    assert "<script" not in html


def test_javascript_link_scheme_is_stripped() -> None:
    html = render_markdown("[click me](javascript:alert(1))")
    assert "javascript:" not in html


def test_ordinary_https_link_is_preserved() -> None:
    html = render_markdown("[the docs](https://example.com/docs)")
    assert 'href="https://example.com/docs"' in html


def test_image_tag_is_stripped() -> None:
    # Not in the allowed-tags set — an `<img onerror=...>` payload smuggled
    # into "plain text" output must not survive as a live element.
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" not in html
