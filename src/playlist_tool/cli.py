"""Phase 1 CLI.

    python -m playlist_tool auth spotify
    python -m playlist_tool auth youtube
    python -m playlist_tool playlists spotify
    python -m playlist_tool dryrun --source spotify --target youtube --playlist "Roadtrip"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from sqlalchemy import select

from .config import REPO_ROOT, load_config
from .core.dryrun import SAVED_TRACKS, DryRunReport, dry_run
from .core.jobs import (
    apply_cached_decisions,
    cache_store,
    create_job,
    fetch_stage,
    match_stage,
    write_stage,
)
from .core.models import Bucket
from .db.models import ItemStatus, JobStatus, TransferItem, TransferJob
from .db.session import get_session
from .providers import get_provider
from .providers.base import ProviderError

console = Console()

BUCKET_STYLE = {
    Bucket.AUTO: "green",
    Bucket.REVIEW: "yellow",
    Bucket.UNMATCHED: "red",
}


def cmd_auth(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    console.print(f"Starting authorization for [bold]{provider.name}[/]…")
    who = provider.authenticate()
    console.print(f"[green]Authorized[/] {provider.name} as [bold]{who}[/]")
    return 0


def cmd_playlists(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    playlists = provider.list_playlists()
    total = len(playlists)

    if args.mine:
        playlists = [p for p in playlists if p.is_owned]

    title = f"{provider.name} playlists ({len(playlists)}"
    title += f" of {total})" if args.mine else ")"
    table = Table(title=title)
    table.add_column("Name", style="bold", max_width=38, overflow="ellipsis")
    table.add_column("Tracks", justify="right")
    table.add_column("Owner", max_width=18, overflow="ellipsis")
    table.add_column("ID", style="dim", no_wrap=True)
    for playlist in playlists:
        table.add_row(
            playlist.name,
            "?" if playlist.track_count is None else str(playlist.track_count),
            playlist.owner or "-",
            playlist.id,
        )
    console.print(table)

    if any(p.track_count is None for p in playlists):
        console.print(
            "[dim]'?' means the platform omits track counts when listing "
            "playlists; the real count is read with the tracks.[/]"
        )

    if provider.name == "spotify":
        console.print(
            "[dim]Note: algorithmic playlists (Discover Weekly, Daily Mix, "
            "Release Radar) are not readable by new Spotify apps and will not "
            "appear here.[/]"
        )
    return 0


def _print_summary(report: DryRunReport) -> None:
    table = Table(title="Match quality")
    table.add_column("Bucket")
    table.add_column("Count", justify="right")
    table.add_column("Share", justify="right")
    for bucket in (Bucket.AUTO, Bucket.REVIEW, Bucket.UNMATCHED):
        rows = report.bucket(bucket)
        share = len(rows) / report.total if report.total else 0.0
        table.add_row(
            f"[{BUCKET_STYLE[bucket]}]{bucket.value}[/]",
            str(len(rows)),
            f"{share:.1%}",
        )
    if report.errors:
        table.add_row("[red]errors[/]", str(len(report.errors)), "")
    console.print(table)

    absent = [r for r in report.results if r.reason == "absent"]
    if absent:
        console.print(
            f"[dim]{len(absent)} of those found nothing resembling the track — "
            f"most likely not in {report.target_provider}'s catalog (DJ sets, "
            "live uploads, non-music video). Review cannot recover these; the "
            "JSON report lists them with reason=\"absent\".[/]"
        )
    console.print(
        f"Auto-match rate [bold]{report.auto_rate:.1%}[/] · "
        f"reachable coverage [bold]{report.coverage:.1%}[/] "
        f"(auto + reviewable) across {report.total} tracks"
    )


def _print_attention(report: DryRunReport) -> None:
    """Show what a human would actually have to look at."""
    # Tracks the target platform does not carry are listed separately: they are
    # a final answer, not a task.
    rows = [
        r
        for r in report.bucket(Bucket.REVIEW) + report.bucket(Bucket.UNMATCHED)
        if r.reason != "absent"
    ]
    if not rows:
        console.print("[green]Nothing needs review.[/]")
        return

    table = Table(title=f"Needs attention ({len(rows)})")
    table.add_column("Source", style="bold", max_width=42)
    table.add_column("Best guess", max_width=42)
    table.add_column("Score", justify="right")
    table.add_column("Why", style="dim", max_width=46)
    for result in sorted(rows, key=lambda r: r.score, reverse=True):
        style = BUCKET_STYLE[result.bucket]
        table.add_row(
            result.source.display(),
            result.best.display() if result.best else "[red]no candidates[/]",
            f"[{style}]{result.score:.2f}[/]",
            result.explain(),
        )
    console.print(table)


def cmd_dryrun(args: argparse.Namespace) -> int:
    source = get_provider(args.source)
    target = get_provider(args.target)

    if args.saved:
        playlist_id, playlist_name = SAVED_TRACKS, "Liked Songs"
        console.print(f"Reading [bold]{playlist_name}[/] from {source.name}…")
    else:
        ref = source.resolve_playlist(args.playlist)
        playlist_id, playlist_name = ref.id, ref.name
        count = "?" if ref.track_count is None else ref.track_count
        console.print(
            f"Reading [bold]{ref.name}[/] ({count} tracks) from {source.name}…"
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Matching against {target.name}", total=None)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, completed=done, total=total)

        report = dry_run(
            source=source,
            target=target,
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            limit=args.limit,
            workers=args.workers,
            on_progress=on_progress,
        )

    _print_summary(report)
    _print_attention(report)

    out_dir = Path(args.out) if args.out else REPO_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() else "-" for c in playlist_name).strip("-")
    out_file = out_dir / f"{stamp}-{source.name}-to-{target.name}-{safe_name}.json"
    out_file.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    console.print(f"\nFull report: [bold]{out_file.relative_to(REPO_ROOT)}[/]")

    if getattr(target, "quota_used", 0):
        console.print(f"[dim]YouTube quota used this run: {target.quota_used} units[/]")
    return 0


def cmd_transfer(args: argparse.Namespace) -> int:
    source = get_provider(args.source)
    target = get_provider(args.target)

    if args.saved:
        playlist_id, playlist_name = SAVED_TRACKS, "Liked Songs"
    else:
        ref = source.resolve_playlist(args.playlist)
        playlist_id, playlist_name = ref.id, ref.name

    job_id = create_job(
        source, target, playlist_id, playlist_name, args.name or playlist_name
    )
    console.print(f"Job [bold]#{job_id}[/] · {playlist_name} → {target.name}")

    added = fetch_stage(source, job_id, limit=args.limit)
    console.print(f"Loaded {added} new track(s) from {source.name}.")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Matching", total=None)

        def on_progress(stage: str, done: int, total: int) -> None:
            progress.update(task, description=stage.title(), completed=done, total=total)

        counts = match_stage(source, target, job_id, workers=args.workers,
                             on_progress=on_progress)

    _print_job_counts(counts)

    if not args.commit:
        console.print(
            "\n[yellow]Nothing written.[/] Re-run with [bold]--commit[/] to create "
            f"the playlist on {target.name}."
        )
        return 0

    ready = counts.get(ItemStatus.MATCHED.value, 0)
    if not ready:
        console.print("[yellow]No confident matches to write.[/]")
        return 0

    # Writing creates a playlist on a real account, so confirm unless told not to.
    if not args.yes:
        console.print(
            f"\nAbout to write [bold]{ready}[/] track(s) to a new "
            f"{target.name} playlist named [bold]{args.name or playlist_name}[/]."
        )
        if getattr(target, "write_batch_size", 50) == 1:
            console.print(
                f"[dim]That costs ~{ready * 50} of YouTube's 10,000 daily quota "
                "units. The job pauses and resumes tomorrow if it runs out.[/]"
            )
        if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
            console.print("Aborted.")
            return 1

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Writing", total=ready)

        def on_write(stage: str, done: int, total: int) -> None:
            progress.update(task, completed=done, total=total)

        result = write_stage(target, job_id, on_progress=on_write)

    console.print(f"[green]Wrote {result['written']} track(s).[/]")
    if result.get("paused"):
        console.print(
            f"[yellow]Paused — YouTube quota exhausted with {result['remaining']} "
            f"left. Re-run 'transfer' tomorrow to resume job #{job_id}.[/]"
        )
    return 0


def _print_job_counts(counts: dict[str, int]) -> None:
    table = Table(title="Job status")
    table.add_column("State")
    table.add_column("Tracks", justify="right")
    for status in ItemStatus:
        if counts.get(status.value):
            table.add_row(status.value, str(counts[status.value]))
    console.print(table)


def cmd_jobs(args: argparse.Namespace) -> int:
    with get_session() as session:
        jobs = list(session.scalars(select(TransferJob).order_by(TransferJob.id.desc())))
        if not jobs:
            console.print("No jobs yet.")
            return 0
        table = Table(title="Transfer jobs")
        table.add_column("#", justify="right")
        table.add_column("Playlist", style="bold", max_width=28, overflow="ellipsis")
        table.add_column("Route")
        table.add_column("Status")
        table.add_column("Written", justify="right")
        table.add_column("Review", justify="right")
        for job in jobs:
            counts = job.counts()
            table.add_row(
                str(job.id),
                job.source_playlist_name,
                f"{job.source_provider}→{job.target_provider}",
                job.status,
                str(counts.get(ItemStatus.WRITTEN.value, 0)),
                str(counts.get(ItemStatus.NEEDS_REVIEW.value, 0)),
            )
        console.print(table)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Resolve ambiguous matches. Decisions are cached and reused forever."""
    with get_session() as session:
        items = list(
            session.scalars(
                select(TransferItem)
                .where(
                    TransferItem.job_id == args.job,
                    TransferItem.status == ItemStatus.NEEDS_REVIEW.value,
                )
                .order_by(TransferItem.position)
            )
        )
        job = session.get(TransferJob, args.job)
        if job is None:
            console.print(f"[red]No job #{args.job}.[/]")
            return 1

        # Apply decisions already made about these tracks in any other job
        # before asking anything, then re-read what genuinely remains.
        reused = apply_cached_decisions(session, args.job)
        if reused:
            session.flush()
            console.print(
                f"[green]{reused} track(s) resolved from earlier decisions.[/]"
            )
            items = [
                item
                for item in items
                if item.status == ItemStatus.NEEDS_REVIEW.value
            ]
            if reused and any(
                i.status == ItemStatus.MATCHED.value for i in job.items
            ):
                job.status = JobStatus.READY.value

        if not items:
            console.print("[green]Nothing to review.[/]")
            return 0

        console.print(f"{len(items)} track(s) to review. Enter = skip, q = quit.\n")
        for item in items:
            candidates = json.loads(item.candidates or "[]")
            console.print(f"[bold]{item.source_label}[/]")
            for index, cand in enumerate(candidates[:4], start=1):
                console.print(f"  {index}. {cand['label']}  [dim]({cand['score']:.2f})[/]")
            if not candidates:
                console.print("  [dim]no candidates[/]")
                continue

            choice = input("  pick [1-4 / Enter / n=none / q]: ").strip().lower()
            if choice == "q":
                break
            if choice == "n":
                item.status = ItemStatus.SKIPPED.value
                item.reason = "user_no_match"
                # Record the negative decision too, so this track is never
                # queued for review again on any playlist.
                cache_store(
                    session,
                    job.source_provider,
                    item.source_track_id,
                    item.source_label,
                    job.target_provider,
                    "",
                    "",
                    item.score,
                    user_verified=True,
                    no_match=True,
                )
                job.status = JobStatus.READY.value
                continue
            if not choice.isdigit() or not (1 <= int(choice) <= len(candidates[:4])):
                continue

            chosen = candidates[int(choice) - 1]
            item.target_track_id = chosen["id"]
            item.target_label = chosen["label"]
            item.status = ItemStatus.MATCHED.value
            item.reason = "user"
            cache_store(
                session,
                job.source_provider,
                item.source_track_id,
                item.source_label,
                job.target_provider,
                chosen["id"],
                chosen["label"],
                item.score,
                user_verified=True,
            )
            console.print("  [green]saved[/]\n")
            # Reopen the job: a review decision creates new work, and leaving it
            # COMPLETED would misreport a job that still has tracks to write.
            job.status = JobStatus.READY.value

    console.print("\nRun 'transfer … --commit' again to write the newly matched tracks.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playlist_tool")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="authorize a provider")
    auth.add_argument("provider", choices=["spotify", "youtube"])
    auth.set_defaults(func=cmd_auth)

    playlists = sub.add_parser("playlists", help="list playlists")
    playlists.add_argument("provider", choices=["spotify", "youtube"])
    playlists.add_argument(
        "--mine", action="store_true", help="only playlists you own, not followed ones"
    )
    playlists.set_defaults(func=cmd_playlists)

    dry = sub.add_parser("dryrun", help="match a playlist without writing anything")
    dry.add_argument("--source", default="spotify")
    dry.add_argument("--target", default="youtube")
    dry.add_argument("--playlist", help="playlist ID, exact name, or unique substring")
    dry.add_argument(
        "--saved", action="store_true", help="use Liked Songs instead of a playlist"
    )
    dry.add_argument("--limit", type=int, help="only match the first N tracks")
    dry.add_argument("--workers", type=int, default=6)
    dry.add_argument("--out", help="directory for the JSON report")
    dry.set_defaults(func=cmd_dryrun)

    transfer = sub.add_parser("transfer", help="match and (with --commit) write")
    transfer.add_argument("--source", default="spotify")
    transfer.add_argument("--target", default="youtube")
    transfer.add_argument("--playlist", help="playlist ID, exact name, or substring")
    transfer.add_argument("--saved", action="store_true", help="use Liked Songs")
    transfer.add_argument("--name", help="name for the target playlist")
    transfer.add_argument("--limit", type=int)
    transfer.add_argument("--workers", type=int, default=6)
    transfer.add_argument(
        "--commit", action="store_true", help="actually write to the target platform"
    )
    transfer.add_argument("--yes", action="store_true", help="skip the confirmation")
    transfer.set_defaults(func=cmd_transfer)

    jobs = sub.add_parser("jobs", help="list transfer jobs")
    jobs.set_defaults(func=cmd_jobs)

    review = sub.add_parser("review", help="resolve ambiguous matches for a job")
    review.add_argument("job", type=int)
    review.set_defaults(func=cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"dryrun", "transfer"} and not args.playlist and not args.saved:
        console.print("[red]Pass --playlist NAME or --saved.[/]")
        return 2
    try:
        return args.func(args)
    except ProviderError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
