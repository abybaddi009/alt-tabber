"""Ranking tests for the palette's fuzzy matcher."""

from alttabber.backends.base import AppInfo, WindowInfo
from alttabber.core.config import PaletteConfig
from alttabber.core.matcher import match


def _win(app: str, title: str, wid: int) -> WindowInfo:
    return WindowInfo(app_name=app, window_title=title, window_id=wid, bundle_id=app)


def _app(name: str) -> AppInfo:
    return AppInfo(name=name, bundle_path=f"/Applications/{name}.app", bundle_id=name)


def test_long_window_title_does_not_dilute_app_name_match():
    # Regression: "teams" scored 60 against the combined "Microsoft Teams
    # Chat | WSD Tickets discussion" text, ranking below unrelated fuzzy
    # hits and falling out of the visible results.
    windows = [
        _win("Stremio", "Stremio - Freedom to Stream", 1),
        _win("System Settings", "Software Update", 2),
        _win("Microsoft Teams", "Chat | WSD Tickets discussion", 3),
        _win("Microsoft Teams", "Microsoft Teams", 4),
    ]
    apps = [_app("TestFlight"), _app("Terminal"), _app("TV")]
    results = match("teams", windows, apps, PaletteConfig())
    top_two = {r.window_id for r in results[:2]}
    assert top_two == {3, 4}


def test_substring_match_in_title_outranks_fuzzy_only_match():
    windows = [
        _win("Firefox", "Quarterly tickets review", 1),
        _win("Stremio", "Stremio - Freedom to Stream", 2),
    ]
    results = match("tickets", windows, [], PaletteConfig())
    assert results[0].window_id == 1
