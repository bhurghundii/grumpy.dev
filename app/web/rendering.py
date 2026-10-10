"""Server-side markdown rendering for the developer-facing templates.

Rendered server-side so the CSP can stay `script-src 'self'`. The text comes
from an LLM fed an attacker-controlled diff, on an unauthenticated page, so
the HTML is sanitised with nh3. That is the only defence against script
injection here. The same applies to the developer's own answers.
"""

from __future__ import annotations

import markdown as _markdown
import nh3
from markupsafe import Markup

_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "a",
}
_ALLOWED_ATTRIBUTES = {"a": {"href"}}

def render_markdown(text: str | None) -> Markup:
    """Converts markdown to sanitized HTML, safe to insert into a template
    unescaped (`{{ text|markdown }}`). Falsy input renders to nothing."""
    if not text:
        return Markup("")

    html = _markdown.markdown(text, extensions=["fenced_code"])
    clean = nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES)
    return Markup(clean)
