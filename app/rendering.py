"""Server-side markdown rendering for text shown on the developer-facing
templates: the tutorial breakdown, grading `reasoning`, and the developer's
own answer text (templates/tutorial.html, result.html, answer.html).

Rendered entirely server-side, on purpose: app/main.py ships a CSP whose
`script-src` is `'self'` alone — one same-origin file, the paste guard
(app/static/nopaste.js), and no inline script at all — and a client-side
markdown library would mean widening that. Converting to HTML here, before
the response goes out, keeps that hardening untouched.

The LLM-generated text this renders ultimately traces back to a PR diff —
attacker-controlled input to the grading model (see app/grading.py's
docstring on why interpretation and comparison are separate calls) — and
`/s/{token}` is unauthenticated, token-in-URL (app/web.py's module
docstring). A crafted diff that tricks the model into echoing
`<script>`/`javascript:` content into what's nominally "plain text" output
becomes a live XSS vector the moment that text is rendered as real HTML
instead of autoescaped by Jinja2. Sanitizing afterward with nh3 is not
optional defense-in-depth here; it's the only thing standing between that
and script injection on a public page. Same bar applies to the developer's
own answer text, since the session link can be opened by more than just
the developer who wrote it.
"""

from __future__ import annotations

import markdown as _markdown
import nh3
from markupsafe import Markup

# Deliberately small: enough for a step-by-step breakdown or a citation-
# backed verdict (headings, emphasis, lists, code, links) and nothing that
# lets a crafted diff smuggle in scripts, styles, or event-handler
# attributes. `a` is restricted to `href` — no `on*` attributes, no
# `target`/`rel` overrides (nh3 sets a safe `rel` itself). URL scheme
# filtering (blocking `javascript:`/`data:` links) and stripping
# `<script>`/`<style>` content entirely are both nh3 defaults, not
# reimplemented here.
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
