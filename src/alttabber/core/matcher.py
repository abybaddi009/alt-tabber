"""Fuzzy matcher that merges open windows and installed-but-unopened apps."""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from ..backends.base import AppInfo, WindowInfo
from .config import PaletteConfig


@dataclass
class Result:
    name: str  # display name (e.g. "Safari — GitHub")
    subtitle: str
    icon_path: str | None
    score: float
    is_window: bool
    window_id: int | None = None
    bundle_id: str | None = None
    bundle_path: str | None = None


def _app_text(a: AppInfo) -> str:
    return a.name


# Floor applied when the query appears verbatim in a field. WRatio scales
# partial matches down by length ratio (to 0.6x once the text is ~8x the
# query), so "teams" scores only 60 against "Microsoft Teams Chat | WSD
# Tickets discussion" -- below unrelated fuzzy hits like "Stremio" (72).
# A literal substring hit is a stronger signal than any fuzzy-only match.
_SUBSTRING_FLOOR = 90.0


def _score(q: str, fields: list[str]) -> float:
    """Best relevance of ``q`` across ``fields`` (already lower-cased).

    Each field is scored on its own as well as combined, so a long window
    title doesn't dilute a strong match on the app name (and vice versa).
    """
    fields = [f for f in fields if f]
    if not fields:
        return 0.0
    candidates = fields + ([" ".join(fields)] if len(fields) > 1 else [])
    best = max(fuzz.WRatio(q, f) for f in candidates)
    if any(q in f for f in fields):
        best = max(best, _SUBSTRING_FLOOR)
    return best


def match(
    query: str,
    windows: list[WindowInfo],
    apps: list[AppInfo],
    cfg: PaletteConfig,
) -> list[Result]:
    q = query.strip().lower()
    open_bundle_ids = {
        w.bundle_id for w in windows if w.bundle_id
    }

    win_results: list[Result] = []
    app_results: list[Result] = []

    for w in windows:
        fields = [w.app_name.lower()]
        if w.window_title and w.window_title != w.app_name:
            fields.append(w.window_title.lower())
        score = _score(q, fields) if q else 0.0
        if q and score < 30:
            continue
        win_results.append(
            Result(
                name=w.window_title or w.app_name,
                subtitle=w.app_name + (" (minimized)" if w.is_minimized else ""),
                icon_path=None,
                score=score,
                is_window=True,
                window_id=w.window_id,
                bundle_id=w.bundle_id,
            )
        )

    for a in apps:
        # Exclude apps that already have an open window.
        if a.bundle_id and a.bundle_id in open_bundle_ids:
            continue
        score = _score(q, [_app_text(a).lower()]) if q else 0.0
        if q and score < 30:
            continue
        app_results.append(
            Result(
                name=a.name,
                subtitle="Application",
                icon_path=a.icon_path,
                score=score,
                is_window=False,
                bundle_id=a.bundle_id,
                bundle_path=a.bundle_path,
            )
        )

    win_results.sort(key=lambda r: r.score, reverse=True)
    app_results.sort(key=lambda r: r.score, reverse=True)

    # When the user is actively searching, rank strictly by relevance so a
    # high-scoring unlaunched app (e.g. "Docker") isn't buried under
    # loosely-matching open windows. With no query, honor windows_first so
    # open windows stay on top during the default browse experience.
    if not q and cfg.windows_first:
        merged = win_results + app_results
    else:
        merged = sorted(
            win_results + app_results, key=lambda r: r.score, reverse=True
        )
    return merged[: cfg.max_results]
