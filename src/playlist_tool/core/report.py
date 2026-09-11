"""Reporting over a persisted job.

Reports are derived from `transfer_items` rather than from a separate in-memory
pass. There used to be two pipelines — a `dry_run()` that fetched and matched in
memory purely to produce a report, alongside the real `fetch_stage` /
`match_stage` — which meant the analysis you inspected was produced by different
code from the transfer you ran. One source of truth removes the chance of them
disagreeing.
"""

from __future__ import annotations

import json

from ..db.models import ItemStatus, TransferJob

#: How a stored item status reads as an outcome.
BUCKET_OF = {
    ItemStatus.MATCHED.value: "matched",
    # Kept distinct from "matched": both mean a target was found, but only this
    # one means the work is already done. Collapsing them makes a re-run look
    # like it has 100 tracks to write when it has none.
    ItemStatus.WRITTEN.value: "written",
    ItemStatus.NEEDS_REVIEW.value: "needs_review",
    ItemStatus.ABSENT.value: "absent",
    ItemStatus.SKIPPED.value: "skipped",
    ItemStatus.FAILED.value: "failed",
    ItemStatus.PENDING.value: "pending",
}

#: Outcomes that mean a target track was successfully identified.
RESOLVED = {"matched", "written"}

BUCKET_STYLE = {
    "matched": "green",
    "written": "green",
    "needs_review": "yellow",
    "absent": "red",
    "skipped": "dim",
    "failed": "red",
    "pending": "dim",
}


def bucket_counts(job: TransferJob) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in job.items:
        key = BUCKET_OF.get(item.status, item.status)
        counts[key] = counts.get(key, 0) + 1
    return counts


def resolvable(job: TransferJob) -> list:
    """Items a human could still act on.

    Tracks the target platform does not carry are excluded: they are a final
    answer, not a task. So are duplicates, which were resolved automatically.
    """
    return [
        item
        for item in job.items
        if item.status in {ItemStatus.NEEDS_REVIEW.value, ItemStatus.FAILED.value}
    ]


def auto_rate(job: TransferJob) -> float:
    total = len(job.items)
    if not total:
        return 0.0
    # Human-resolved tracks are excluded: this measures what the matcher got
    # right on its own, which is the number worth reporting.
    matched = sum(
        1
        for item in job.items
        if BUCKET_OF.get(item.status) in RESOLVED and item.reason != "user"
    )
    return matched / total


def coverage(job: TransferJob) -> float:
    """Share reachable if every ambiguous track were resolved by hand."""
    total = len(job.items)
    if not total:
        return 0.0
    reachable = sum(
        1
        for item in job.items
        if BUCKET_OF.get(item.status) in RESOLVED | {"needs_review"}
    )
    return reachable / total


def to_dict(job: TransferJob) -> dict:
    counts = bucket_counts(job)
    return {
        "job": job.id,
        "source": job.source_provider,
        "target": job.target_provider,
        "playlist": {
            "id": job.source_playlist_id,
            "name": job.source_playlist_name,
            "target_id": job.target_playlist_id,
        },
        "summary": {
            "total": len(job.items),
            **counts,
            "auto_rate": round(auto_rate(job), 4),
            "coverage": round(coverage(job), 4),
        },
        "tracks": [
            {
                "position": item.position,
                "source": item.source_label,
                "source_id": item.source_track_id,
                "isrc": item.source_isrc,
                "duration_ms": item.source_duration_ms,
                "status": item.status,
                "bucket": BUCKET_OF.get(item.status, item.status),
                "reason": item.reason,
                "score": round(item.score, 4),
                "match": item.target_label or None,
                "match_id": item.target_track_id,
                "alternatives": json.loads(item.candidates or "[]"),
                "error": item.error,
            }
            for item in sorted(job.items, key=lambda i: i.position)
        ],
    }
