"""Builds /dashboard and its CSV exports from query parameters (see argus_herald.dashboard)."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from datetime import date
from urllib.parse import urlencode, urlparse

from argus_herald import clip_panel
from argus_herald import dashboard as d
from argus_herald.charts import series_panel, stacked_area
from argus_herald.config import Config
from argus_herald.metrics import metrics_path
from argus_herald.store import Store

RANGES = (30, 90, 180, 365)
TABLES = ("timeline", "followers", "referrers", "broadcasts", "platforms")
REPORT_FILTERS = ("platform", "kind", "media", "drafted", "logged")


def own_hosts(config: Config) -> tuple[str, ...]:
    urls = [config.digest.site_url] if config.digest and config.digest.site_url else []
    urls += [getattr(f, "url", "") for f in config.feeds if getattr(f, "name", "") == "site"]
    return tuple(sorted({urlparse(u).hostname or "" for u in urls} - {""}))


def default_week_start(config: Config) -> int:
    return config.digest.anchor.weekday() if config.digest else 0


def filters(config: Config, params: Mapping[str, str], conn) -> d.Filters:
    try:
        days = int(params.get("days") or 90)
    except ValueError:
        days = 90
    project = params.get("project") or ""
    if project not in d.projects(conn):
        project = ""
    week = params.get("week") or ""
    week_start = d.WEEKDAYS.index(week) if week in d.WEEKDAYS else default_week_start(config)
    return d.Filters(days=days if days in RANGES else 90, project=project, week_start=week_start)


def _report(
    conn, config: Config, f: d.Filters, params: Mapping[str, str]
) -> tuple[list[d.Broadcast], list[d.Broadcast]]:
    everything = d.broadcast_report(conn, f, own_hosts(config))
    shown = d.filter_report(
        everything,
        platform=params.get("platform") or "",
        kind=params.get("kind") or "",
        media=params.get("media") or "",
        drafted_by=params.get("drafted") or "",
        logged=params.get("logged") or "",
    )
    key = params.get("sort") or "at"
    if key in d.SORT_KEYS:
        shown = sorted(shown, key=d.SORT_KEYS[key], reverse=(params.get("dir") or "desc") == "desc")
    return everything, shown


def context(config: Config, params: Mapping[str, str]) -> dict:
    path = metrics_path(config)
    conn = d.connect(path)
    try:
        f = filters(config, params, conn)
        log = params.get("scale") == "log"
        dates = f.dates()
        series = d.timeline(conn, f)
        rel = d.releases(conn, f)
        everything, report = _report(conn, config, f, params)
        markers = d.filter_report(everything, platform=params.get("platform") or "")
        panels = [
            {
                "key": key,
                "title": title,
                "values": series[key],
                "svg": series_panel(key, title, dates, series[key], d.rolling_mean(series[key]), rel, markers, log=log),
            }
            for key, title in d.SERIES
            if not (key == "visits" and f.project)
        ]
        audience = d.followers(conn, f)
        follower_panels = [
            {
                "key": f"followers-{p}",
                "title": f"{d.platform_label(p)} followers",
                "svg": series_panel(
                    f"followers-{p}",
                    f"{d.platform_label(p)} followers",
                    dates,
                    values,
                    None,
                    [],
                    markers,
                    log=log,
                    slot=d.platform_slot(p),
                    height=110,
                    zero=False,
                ),
            }
            for p, values in sorted(audience.items(), key=lambda kv: d.platform_slot(kv[0]))
        ]
        ref_days, ref_series = d.referrers(conn, f, own_hosts(config))
        groups = [(g, label, slot, ref_series[g]) for g, label, slot in d.REFERRER_GROUPS if g in ref_series]
        summary = d.platform_summary(conn, everything, f)
        project_names = d.projects(conn)
    finally:
        if conn is not None:
            conn.close()
    present = sorted({d.platform_key(b.platform) for b in everything}, key=d.platform_slot)
    store = Store(config.store_path)
    try:
        clips = clip_panel.load(store, path, config.clips.performance)
    finally:
        store.close()

    def link(**changes: str) -> str:
        merged = {k: v for k, v in params.items() if v}
        merged.update(changes)
        return "/dashboard?" + urlencode({k: v for k, v in merged.items() if v})

    def sort_link(key: str) -> str:
        current, direction = params.get("sort") or "at", params.get("dir") or "desc"
        return link(sort=key, dir="asc" if current == key and direction == "desc" else "desc")

    return {
        "has_metrics": path.exists(),
        "f": f,
        "params": dict(params),
        "ranges": RANGES,
        "log": log,
        "projects": project_names,
        "weekdays": d.WEEKDAYS,
        "tiles": d.summary_tiles(series),
        "panels": panels,
        "follower_panels": follower_panels,
        "dates": dates,
        "series": series,
        "audience": audience,
        "legend": [{"key": p, "label": d.platform_label(p), "slot": d.platform_slot(p)} for p in present],
        "has_releases": bool(rel),
        "ref_svg": stacked_area(ref_days, groups),
        "ref_days": ref_days,
        "ref_groups": groups,
        "report": report,
        "report_total": len(everything),
        "delta_metrics": d.DELTA_METRICS,
        "kinds": sorted({b.format for b in everything}),
        "backends": sorted({b.drafted_by.split(":")[0] for b in everything}),
        "summary": summary,
        "clips": clips,
        "platform_label": d.platform_label,
        "platform_slot": d.platform_slot,
        "link": link,
        "sort_link": sort_link,
        "csv_query": urlencode({k: v for k, v in params.items() if v}),
        "tables": TABLES,
        "report_filters": REPORT_FILTERS,
    }


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def export(config: Config, table: str, params: Mapping[str, str]) -> str:
    """One table as CSV, with the same filters as the page."""
    ctx = context(config, params)
    out = io.StringIO()
    w = csv.writer(out)
    if table == "timeline":
        keys = [k for k, _ in d.SERIES]
        w.writerow(["date", *keys, *(f"{k}_7d_mean" for k in keys)])
        means = {k: d.rolling_mean(ctx["series"][k]) for k in keys}
        for i, day in enumerate(ctx["dates"]):
            w.writerow(
                [day.isoformat(), *(_fmt(ctx["series"][k][i]) for k in keys), *(_fmt(means[k][i]) for k in keys)]
            )
    elif table == "followers":
        platforms = list(ctx["audience"])
        w.writerow(["date", *platforms])
        for i, day in enumerate(ctx["dates"]):
            w.writerow([day.isoformat(), *(_fmt(ctx["audience"][p][i]) for p in platforms)])
    elif table == "referrers":
        w.writerow(["snapshot_date", *(label for _g, label, _s, _v in ctx["ref_groups"])])
        for i, day in enumerate(ctx["ref_days"]):
            w.writerow([day, *(_fmt(v[i]) for _g, _l, _s, v in ctx["ref_groups"])])
    elif table == "broadcasts":
        keys = [k for k, _ in d.DELTA_METRICS]
        w.writerow(
            [
                "at",
                "platform",
                "format",
                "subject",
                "url",
                "logged",
                "drafted_by",
                "media",
                "overlap",
                "engagement_24h",
                "engagement_7d",
                "impressions",
                *(f"{k}_delta_per_day" for k in keys),
                *(f"{k}_baseline_median" for k in keys),
            ]
        )
        for b in ctx["report"]:
            w.writerow(
                [
                    b.at.isoformat(),
                    b.platform,
                    b.format,
                    b.subject,
                    b.url,
                    b.logged,
                    b.drafted_by,
                    b.media,
                    b.overlap,
                    _fmt(b.engagement_24h),
                    _fmt(b.engagement_7d),
                    _fmt(b.impressions),
                    *(_fmt(b.deltas.get(k)) for k in keys),
                    *(_fmt(b.baselines.get(k)) for k in keys),
                ]
            )
    elif table == "platforms":
        w.writerow(
            ["week", "platform", "posts", "engagement", "posts_with_engagement", "referrer_uniques", "follower_change"]
        )
        for r in ctx["summary"]:
            w.writerow(
                [
                    r["week"].isoformat(),
                    r["platform"],
                    r["posts"],
                    r["engagement"],
                    r["engaged_posts"],
                    _fmt(r["referrers"]),
                    _fmt(r["followers"]),
                ]
            )
    else:
        raise KeyError(table)
    return out.getvalue()
