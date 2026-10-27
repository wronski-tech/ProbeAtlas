"""Query-time aggregation over raw results.

Aggregates are computed on demand and never written back into the raw results
table. Percentiles are only reported when there are enough successful samples
to be meaningful; otherwise they are ``null``.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from .schemas import GroupPerformance, LatencyStats, RegionalPerformance, Timeseries, TimeseriesPoint, TimeseriesSeries

MIN_SAMPLES = {"p50": 5, "p95": 20, "p99": 100}
DEGRADED_SUCCESS_RATE = 0.95
SLOW_FACTOR = 2.0

def agg_columns(latency_filter: str = "success") -> str:
    """Aggregate columns; latency statistics only consider rows matching ``latency_filter``."""
    f = latency_filter
    return f"""
    count(*) AS total,
    count(*) FILTER (WHERE success) AS successes,
    count(latency_ms) FILTER (WHERE {f}) AS n_lat,
    min(latency_ms) FILTER (WHERE {f}) AS min_ms,
    avg(latency_ms) FILTER (WHERE {f}) AS avg_ms,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE {f}) AS p50,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE {f}) AS p95,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE {f}) AS p99,
    avg(packet_loss_pct) AS loss_avg,
    max(measured_at) AS last_at
"""



def _r(v: float | None) -> float | None:
    """Round to 2 decimals, preserving ``None``."""
    return None if v is None else round(float(v), 2)


def latency_stats(row) -> LatencyStats:
    """Build latency statistics from an aggregate row, nulling percentiles without enough samples."""
    n = row.n_lat or 0
    return LatencyStats(
        sample_count=n,
        min_ms=_r(row.min_ms),
        avg_ms=_r(row.avg_ms),
        p50_ms=_r(row.p50) if n >= MIN_SAMPLES["p50"] else None,
        p95_ms=_r(row.p95) if n >= MIN_SAMPLES["p95"] else None,
        p99_ms=_r(row.p99) if n >= MIN_SAMPLES["p99"] else None,
    )


def group_performance(row, key: str, region: str, missing: int, agent_id=None, agent_name=None) -> GroupPerformance:
    """Build the performance summary for one region or agent from an aggregate row.

    ``missing`` is the number of scheduled jobs that expired without a result; ``insufficient_data`` is set
    when there are fewer than ``MIN_SAMPLES["p50"]`` latency samples.
    """
    total = row.total or 0
    return GroupPerformance(
        key=key,
        region=region,
        agent_id=agent_id,
        agent_name=agent_name,
        total=total,
        successes=row.successes or 0,
        success_rate=round(row.successes / total, 4) if total else None,
        missing=missing,
        latency=latency_stats(row),
        packet_loss_avg_pct=_r(row.loss_avg),
        last_measured_at=row.last_at,
        insufficient_data=(row.n_lat or 0) < MIN_SAMPLES["p50"],
    )


class _Empty:
    """Stand-in aggregate row for a group with no results."""

    total = successes = n_lat = 0
    min_ms = avg_ms = p50 = p95 = p99 = loss_avg = last_at = None


def regional_performance(
    db: Session, monitor_id: uuid.UUID, regions: list[str], since: datetime, until: datetime,
    region: str | None = None, agent_id: uuid.UUID | None = None,
) -> RegionalPerformance:
    """Compare a monitor's performance across regions and agents for a time window.

    Returns overall, per-region, and per-agent summaries, failure category counts, missing-data counts, and a
    global/regional assessment. Configured regions without results are included with zero samples.
    """
    params = {"mid": monitor_id, "since": since, "until": until, "region": region, "agent": agent_id}
    filt = ("monitor_id = :mid AND measured_at >= :since AND measured_at < :until"
            " AND (CAST(:region AS text) IS NULL OR region = :region)"
            " AND (CAST(:agent AS uuid) IS NULL OR agent_id = :agent)")
    rows = db.execute(
        text(
            f"""
            SELECT region, agent_id, GROUPING(region, agent_id) AS lvl, {agg_columns()}
            FROM results WHERE {filt}
            GROUP BY GROUPING SETS ((region, agent_id), (region), ())
            """
        ),
        params,
    ).all()
    missing_rows = db.execute(
        text(
            """
            SELECT region, agent_id, count(*) AS n FROM jobs
            WHERE monitor_id = :mid AND status = 'expired' AND scheduled_for >= :since AND scheduled_for < :until
              AND (CAST(:region AS text) IS NULL OR region = :region)
            GROUP BY region, agent_id
            """
        ),
        params,
    ).all()
    agent_names = dict(db.execute(text("SELECT id, name FROM agents")).all())
    failures = dict(
        db.execute(
            text(f"SELECT failure_category, count(*) FROM results WHERE {filt} AND NOT success "
                 "GROUP BY failure_category ORDER BY 2 DESC"),
            params,
        ).all()
    )

    missing_region: dict[str, int] = {}
    missing_agent: dict[tuple[str, uuid.UUID], int] = {}
    for m in missing_rows:
        missing_region[m.region] = missing_region.get(m.region, 0) + m.n
        if m.agent_id:
            missing_agent[(m.region, m.agent_id)] = m.n

    overall = group_performance(_Empty, "all", "all", sum(missing_region.values()))
    by_region: dict[str, GroupPerformance] = {}
    agents: list[GroupPerformance] = []
    for row in rows:
        if row.lvl == 3:
            overall = group_performance(row, "all", "all", sum(missing_region.values()))
        elif row.lvl == 1:
            by_region[row.region] = group_performance(row, row.region, row.region, missing_region.get(row.region, 0))
        else:
            agents.append(group_performance(row, f"{row.region}/{row.agent_id}", row.region,
                                 missing_agent.get((row.region, row.agent_id), 0), row.agent_id,
                                 agent_names.get(row.agent_id)))
    # Regions that are configured but produced no results are reported explicitly.
    for reg in regions if region is None else [region]:
        if reg not in by_region:
            by_region[reg] = group_performance(_Empty, reg, reg, missing_region.get(reg, 0))

    region_list = sorted(by_region.values(), key=lambda g: g.region)
    assessment, slow = assess(region_list)
    return RegionalPerformance(
        monitor_id=monitor_id,
        since=since,
        until=until,
        overall=overall,
        regions=region_list,
        agents=sorted(agents, key=lambda g: g.key),
        failure_categories={k or "unknown": v for k, v in failures.items()},
        assessment=assessment,
        slow_regions=slow,
    )


def assess(regions: list[GroupPerformance]) -> tuple[str, list[str]]:
    """Classify availability problems as global or regional and flag slow regions."""
    with_data = [g for g in regions if g.total > 0]
    if not with_data:
        return "no_data", []
    degraded = [g for g in with_data if (g.success_rate or 0) < DEGRADED_SUCCESS_RATE]
    if not degraded:
        # Silence is not health: flag regions where most scheduled checks never ran.
        assessment = "incomplete" if any(g.missing and g.missing >= g.total for g in regions) else "healthy"
    elif len(degraded) == len(with_data) and len(with_data) > 1:
        assessment = "global"
    elif len(with_data) == 1:
        assessment = "degraded"
    else:
        assessment = "regional"
    p50s = {g.region: g.latency.p50_ms for g in with_data if g.latency.p50_ms is not None}
    slow: list[str] = []
    if len(p50s) >= 2:
        for reg, v in p50s.items():
            others = [x for r, x in p50s.items() if r != reg]
            if v > SLOW_FACTOR * statistics.median(others):
                slow.append(reg)
    return assessment, sorted(slow)


def choose_bucket(since: datetime, until: datetime, target_points: int = 60) -> int:
    """Pick a bucket size in seconds that gives about ``target_points`` points over the window."""
    span = max((until - since).total_seconds(), 60)
    raw = span / target_points
    for b in (60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400):
        if b >= raw:
            return b
    return 86400


def timeseries(
    db: Session, monitor_id: uuid.UUID, since: datetime, until: datetime, group_by: str,
    region: str | None = None, agent_id: uuid.UUID | None = None, bucket_seconds: int | None = None,
) -> Timeseries:
    """Return bucketed success counts and latency statistics for a monitor.

    Series are grouped by ``region``, ``agent``, or ``none``. Percentiles are null in buckets with too few
    samples.
    """
    bucket = bucket_seconds or choose_bucket(since, until)
    key = {"region": "region", "agent": "CAST(agent_id AS text)", "none": "'all'"}[group_by]
    rows = db.execute(
        text(
            f"""
            SELECT date_bin(make_interval(secs => :bucket), measured_at, TIMESTAMPTZ '2000-01-01') AS bucket,
                   {key} AS k,
                   count(*) AS total,
                   count(*) FILTER (WHERE success) AS successes,
                   count(latency_ms) FILTER (WHERE success) AS n_lat,
                   avg(latency_ms) FILTER (WHERE success) AS avg_ms,
                   max(latency_ms) FILTER (WHERE success) AS max_ms,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE success) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE success) AS p95
            FROM results
            WHERE monitor_id = :mid AND measured_at >= :since AND measured_at < :until
              AND (CAST(:region AS text) IS NULL OR region = :region)
              AND (CAST(:agent AS uuid) IS NULL OR agent_id = :agent)
            GROUP BY 1, 2 ORDER BY 1
            """
        ),
        {"bucket": bucket, "mid": monitor_id, "since": since, "until": until, "region": region,
         "agent": agent_id},
    ).all()
    series: dict[str, list[TimeseriesPoint]] = {}
    for row in rows:
        n = row.n_lat or 0
        series.setdefault(row.k or "unknown", []).append(
            TimeseriesPoint(
                t=row.bucket,
                total=row.total,
                successes=row.successes,
                avg_ms=_r(row.avg_ms),
                max_ms=_r(row.max_ms),
                p50_ms=_r(row.p50) if n >= MIN_SAMPLES["p50"] else None,
                p95_ms=_r(row.p95) if n >= MIN_SAMPLES["p95"] else None,
            )
        )
    return Timeseries(
        monitor_id=monitor_id,
        since=since,
        until=until,
        bucket_seconds=bucket,
        series=[TimeseriesSeries(key=k, points=v) for k, v in sorted(series.items())],
    )


def parse_window(since: datetime | None, until: datetime | None, default_hours: int = 24) -> tuple[datetime, datetime]:
    """Resolve an optional ``since``/``until`` window to UTC, defaulting to the last ``default_hours``.

    Raises 422 if the window is empty, reversed, or longer than 90 days.
    """
    from datetime import UTC

    from .errors import APIError

    until = (until or datetime.now(UTC)).astimezone(UTC)
    since = (since or until - timedelta(hours=default_hours)).astimezone(UTC)
    if since >= until:
        raise APIError(422, "validation_error", "`since` must be before `until`")
    if until - since > timedelta(days=90):
        raise APIError(422, "validation_error", "time window may not exceed 90 days")
    return since, until
