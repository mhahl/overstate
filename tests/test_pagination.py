"""Pagination standard: shared sizes, validated default, size dropdown.

Every data table pages 50 by default with 10/25/50/100 options. Sizes
are validated centrally (minions.resolve_page_size); the dropdown
macro preserves the surrounding query params.
"""

from overstate_ui import minions


def test_page_sizes_cover_ten_to_hundred():
    assert minions.PAGE_SIZES == (10, 25, 50, 100)
    assert minions.DEFAULT_PAGE_SIZE == 50


def test_resolve_page_size_accepts_only_known_sizes():
    assert minions.resolve_page_size("10") == 10
    assert minions.resolve_page_size("100") == 100
    assert minions.resolve_page_size("999") == 50
    assert minions.resolve_page_size("abc") == 50
    assert minions.resolve_page_size(None) == 50
    assert minions.resolve_page_size("25", default=10) == 25
    assert minions.resolve_page_size("999", default=10) == 10


def test_page_size_dropdown_renders_options_and_selection():
    import pathlib

    from jinja2 import Environment, FileSystemLoader

    templates = (
        pathlib.Path(__file__).resolve().parent.parent
        / "overstate_ui"
        / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    env.globals["url_for"] = lambda endpoint, **params: "/" + endpoint.replace(
        ".", "/"
    )
    rendered = env.from_string(
        '{% import "_macros.html" as m %}'
        '{{ m.page_size_select("vulns.index", 50, {"q": "web"}) }}'
    ).render()
    assert 'name="per_page"' in rendered
    assert "<label" in rendered
    for size in ("10", "25", "50", "100"):
        assert f'value="{size}"' in rendered
    assert "selected" in rendered
    assert 'name="q" value="web"' in rendered
    assert "requestSubmit()" in rendered
