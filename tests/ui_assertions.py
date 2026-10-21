"""Structural UI assertions shared by the template test suites.

These parse rendered HTML rather than matching markup strings, so a
layout rule can be enforced across every page without pinning exact
class order or nesting.
"""

from html.parser import HTMLParser

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _SearchCardCheck(HTMLParser):
    """Find ``role="search"`` forms with no card surface above them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._open: list[tuple[str, set[str]]] = []
        self.offenders: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        # A toolbar belongs on a raised card surface, not the grey page
        # background. Mirror the list pages' shared shell.
        if (
            tag == "form"
            and attr.get("role") == "search"
            and not any(
                "card" in classes and "bg-base-100" in classes
                for _tag, classes in self._open
            )
        ):
            label = attr.get("aria-label") or "search form"
            self.offenders.append(label)
        if tag not in _VOID:
            self._open.append((tag, set((attr.get("class") or "").split())))

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                return


def searches_off_card(html: str) -> list[str]:
    """Labels of search forms rendered outside a ``card bg-base-100``."""
    check = _SearchCardCheck()
    check.feed(html)
    return check.offenders
