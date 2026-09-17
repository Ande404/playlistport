"""Phase 1 CLI.

    python -m playlistport auth spotify
    python -m playlistport auth youtube
    python -m playlistport playlists spotify
    python -m playlistport dryrun --source spotify --target youtube --playlist "Roadtrip"
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
from .core import report
from .core.dedupe import find_duplicates, group_duplicates
from .core.jobs import (
    apply_cached_decisions,
    cache_store,
    create_job,
    drain_jobs,
    fetch_stage,
    finalize_job,
    pending_write_queue,
    match_stage,
    write_stage,
)
from .core.models import SAVED_TRACKS
from .db.models import ItemStatus, JobStatus, TransferItem, TransferJob
from .db.session import get_session
from .providers import get_provider
from .providers.base import ProviderError

console = Console()



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


def _print_summary(job) -> None:
    counts = report.bucket_counts(job)
    total = len(job.items)

    table = Table(title="Match quality")
    table.add_column("Outcome")
    table.add_column("Tracks", justify="right")
    table.add_column("Share", justify="right")
    for bucket in (
        "matched",
        "written",
        "needs_review",
        "absent",
        "skipped",
        "failed",
        "pending",
    ):
        if not counts.get(bucket):
            continue
        style = report.BUCKET_STYLE[bucket]
        table.add_row(
            f"[{style}]{bucket}[/]",
            str(counts[bucket]),
            f"{counts[bucket] / total:.1%}" if total else "-",
        )
    console.print(table)

    if counts.get("absent"):
        console.print(
            f"[dim]{counts['absent']} found nothing resembling the track — most "
            f"likely not in {job.target_provider}'s catalog (DJ sets, live "
            "uploads, non-music video). Review cannot recover these.[/]"
        )
    console.print(
        f"Auto-match rate [bold]{report.auto_rate(job):.1%}[/] · "
        f"reachable coverage [bold]{report.coverage(job):.1%}[/] "
        f"(auto + reviewable) across {total} tracks"
    )


def _print_attention(job) -> None:
    """Show what a human would actually have to look at."""
    rows = report.resolvable(job)
    if not rows:
        console.print("[green]Nothing needs review.[/]")
        return

    table = Table(title=f"Needs attention ({len(rows)})")
    table.add_column("Source", style="bold", max_width=42)
    table.add_column("Best guess", max_width=42)
    table.add_column("Score", justify="right")
    table.add_column("Why", style="dim", max_width=30)
    for item in sorted(rows, key=lambda i: i.score, reverse=True):
        table.add_row(
            item.source_label,
            item.target_label or "[red]no candidates[/]",
            f"{item.score:.2f}",
            item.error or item.reason or "",
        )
    console.print(table)


def _write_report(job, out: str | None) -> None:
    out_dir = Path(out) if out else REPO_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "-" for c in job.source_playlist_name)
    path = (
        out_dir
        / f"{stamp}-{job.source_provider}-to-{job.target_provider}-{safe.strip('-')}.json"
    )
    path.write_text(json.dumps(report.to_dict(job), indent=2, ensure_ascii=False))
    try:
        shown = path.relative_to(REPO_ROOT)
    except ValueError:
        shown = path
    console.print(f"\nFull report: [bold]{shown}[/]")


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

    with get_session() as session:
        job = session.get(TransferJob, job_id)
        _print_summary(job)
        if args.verbose or not args.commit:
            _print_attention(job)
        if args.report is not None:
            _write_report(job, args.report or None)

    if not args.commit:
        console.print(
            "\n[yellow]Nothing written.[/] Re-run with [bold]--commit[/] to create "
            f"the playlist on {target.name}."
        )
        return 0

    ready = counts.get(ItemStatus.MATCHED.value, 0)
    if not ready:
        status = finalize_job(job_id)
        if status == JobStatus.COMPLETED.value:
            console.print("[green]Nothing left to write — job complete.[/]")
        else:
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


def cmd_dedupe(args: argparse.Namespace) -> int:
    """Remove repeated occurrences of the same track from a playlist."""
    provider = get_provider(args.provider)
    if not provider.supports_removal:
        console.print(f"[red]{provider.name} does not support removal.[/]")
        return 1

    ref = provider.resolve_playlist(args.playlist)
    console.print(f"Reading [bold]{ref.name}[/] from {provider.name}…")
    entries = provider.list_entries(ref.id)
    groups = group_duplicates(entries)
    extras = find_duplicates(entries)

    if not extras:
        console.print(
            f"[green]No duplicates.[/] {len(entries)} entries, all distinct."
        )
        return 0

    table = Table(title=f"Duplicates in {ref.name}")
    table.add_column("Track", style="bold", max_width=46, overflow="ellipsis")
    table.add_column("Copies", justify="right")
    table.add_column("Keep", justify="right")
    table.add_column("Remove", justify="right")
    for occurrences in groups.values():
        table.add_row(
            occurrences[0].label or occurrences[0].track_id,
            str(len(occurrences)),
            str(occurrences[0].position),
            ", ".join(str(o.position) for o in occurrences[1:]),
        )
    console.print(table)
    console.print(
        f"{len(entries)} entries · {len(groups)} track(s) duplicated · "
        f"[bold]{len(extras)}[/] occurrence(s) would be removed. "
        "The earliest copy of each is kept."
    )

    if not args.commit:
        console.print("\n[yellow]Nothing removed.[/] Re-run with [bold]--commit[/].")
        return 0

    # Deletion is irreversible and, on YouTube, expensive.
    if not args.yes:
        cost = ""
        if provider.name == "youtube":
            cost = f" That costs ~{len(extras) * 50} YouTube quota units."
        console.print(
            f"\n[bold]Permanently remove {len(extras)} occurrence(s)[/] from "
            f"{ref.name} on {provider.name}?{cost}"
        )
        if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
            console.print("Aborted.")
            return 1

    provider.remove_entries(ref.id, extras)
    console.print(
        f"[green]Removed {len(extras)} occurrence(s)[/] from {ref.name}. "
        f"{len(entries) - len(extras)} entries remain."
    )
    # Deliberately not re-listing to confirm: a read immediately after deletion
    # returns a stale view (observed reporting 4 duplicates still present when a
    # fresh read seconds later showed none). Verifying against eventually
    # consistent data produces a confidently wrong answer, which is worse than
    # not verifying at all.
    console.print(
        "[dim]Re-run without --commit to verify; the platform takes a moment to "
        "reflect deletions.[/]"
    )
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    """Write every queued job until the daily quota is exhausted.

    Built for unattended runs. A scheduler invoking one playlist per day would
    leave most of the budget unused — `reg` costs 2,550 of 10,000 — so this
    works through the queue and stops when the platform says no more.
    """
    target = get_provider(args.target)
    queue = pending_write_queue(target.name)

    if not queue:
        console.print("[green]Nothing queued to write.[/]")
        return 0

    console.print(
        f"{len(queue)} job(s) queued · {sum(n for n, _, _ in queue)} track(s) · "
        f"~{sum(n for n, _, _ in queue) * 50} quota units"
    )
    if not args.commit:
        for count, job_id, name in queue:
            console.print(f"  job#{job_id} {name[:32]:34} {count:4} tracks")
        console.print("\n[yellow]Nothing written.[/] Re-run with [bold]--commit[/].")
        return 0

    def on_job(name: str, job_id: int, result: dict) -> None:
        console.print(f"  {name[:34]:36} wrote {result['written']}")

    console.print("")
    outcome = drain_jobs(target, on_job=on_job)

    if outcome.get("auth_required"):
        console.print(
            f"\n[red]Stopped during {outcome['paused_on']}: authorization "
            "required.[/] Run 'playlistport auth youtube' in a terminal."
        )
    elif outcome["paused_on"]:
        console.print(
            f"\n[yellow]Quota exhausted during {outcome['paused_on']}.[/] "
            f"{outcome['remaining']} track(s) left there, "
            f"{outcome['jobs_untouched']} job(s) untouched. Re-run after the reset."
        )
    else:
        console.print("\n[green]Queue drained.[/]")
    console.print(
        f"[bold]{outcome['written']}[/] track(s) written · "
        f"{len(outcome['completed'])} playlist(s) completed"
    )
    return 0


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
    parser = argparse.ArgumentParser(prog="playlistport")
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

    transfer = sub.add_parser(
        "transfer",
        help="match a playlist; writes only with --commit",
        description=(
            "Matches a playlist against the target platform and saves the result "
            "as a resumable job. Nothing is written to the target platform unless "
            "--commit is given."
        ),
    )
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
    transfer.add_argument(
        "--report",
        nargs="?",
        const="",
        metavar="DIR",
        help="write a JSON match-quality report (default: ./reports)",
    )
    transfer.add_argument(
        "--verbose",
        action="store_true",
        help="list ambiguous tracks even when committing",
    )
    transfer.set_defaults(func=cmd_transfer)

    jobs = sub.add_parser("jobs", help="list transfer jobs")
    jobs.set_defaults(func=cmd_jobs)

    drain = sub.add_parser(
        "drain",
        help="write all queued jobs until the daily quota runs out",
        description=(
            "Writes every already-matched job, cheapest first, until the target "
            "platform's daily quota is exhausted. Intended for scheduled runs. "
            "Nothing is written unless --commit is given."
        ),
    )
    drain.add_argument("--target", default="youtube")
    drain.add_argument(
        "--commit", action="store_true", help="actually write to the target platform"
    )
    drain.set_defaults(func=cmd_drain)

    dedupe = sub.add_parser(
        "dedupe",
        help="remove repeated occurrences of a track from a playlist",
        description=(
            "Finds tracks appearing more than once in a playlist and removes the "
            "extra occurrences, keeping the earliest. Nothing is removed unless "
            "--commit is given."
        ),
    )
    dedupe.add_argument("--provider", default="youtube")
    dedupe.add_argument(
        "--playlist", required=True, help="playlist ID, exact name, or substring"
    )
    dedupe.add_argument(
        "--commit", action="store_true", help="actually remove the duplicates"
    )
    dedupe.add_argument("--yes", action="store_true", help="skip the confirmation")
    dedupe.set_defaults(func=cmd_dedupe)

    review = sub.add_parser("review", help="resolve ambiguous matches for a job")
    review.add_argument("job", type=int)
    review.set_defaults(func=cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "transfer" and not args.playlist and not args.saved:
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
