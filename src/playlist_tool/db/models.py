"""Persistence schema.

Three properties fall out of this schema rather than being coded around:

* **Resume** — `TransferItem` holds one durable row per track per job, so a
  killed process resumes exactly where it stopped.
* **Idempotency** — an item at `WRITTEN` is never written again, and
  `MatchCache` means a track already resolved is never re-searched.
* **Sync** — `PlaylistLink` remembers which target playlist belongs to which
  source playlist, so a re-run appends only what is new.

`user_id` exists from day one on the tables that would need it, defaulting to
`"local"`. Multi-user then becomes a migration rather than a rewrite.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

LOCAL_USER = "local"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus(str, Enum):
    PENDING = "pending"
    MATCHING = "matching"
    READY = "ready"          # matched, awaiting --commit
    WRITING = "writing"
    PAUSED_QUOTA = "paused_quota"
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStatus(str, Enum):
    PENDING = "pending"
    MATCHED = "matched"            # confident, queued for write
    NEEDS_REVIEW = "needs_review"  # ambiguous, awaiting a human
    ABSENT = "absent"              # not on the target platform at all
    WRITTEN = "written"
    FAILED = "failed"
    SKIPPED = "skipped"


class MatchCache(Base):
    """Track-to-track mappings, reused across every playlist and job.

    Rows confirmed by a human (`user_verified`) are authoritative and are never
    overwritten by a later automatic match — that is what makes review
    corrections permanent.
    """

    __tablename__ = "match_cache"
    __table_args__ = (
        UniqueConstraint(
            "source_provider", "source_id", "target_provider", name="uq_match_pair"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_provider: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(191), index=True)
    target_provider: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(191))

    score: Mapped[float] = mapped_column(Float, default=0.0)
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    #: A recorded *negative* decision: the user determined this track has no
    #: counterpart on the target platform. Without it, answering "no match"
    #: resolves only the row in front of you, and the same question returns in
    #: the next job. `target_id` is empty for these.
    no_match: Mapped[bool] = mapped_column(Boolean, default=False)
    source_label: Mapped[str] = mapped_column(String(512), default="")
    target_label: Mapped[str] = mapped_column(String(512), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PlaylistLink(Base):
    """Source playlist <-> target playlist. The basis of ongoing sync."""

    __tablename__ = "playlist_links"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_provider",
            "source_playlist_id",
            "target_provider",
            name="uq_link",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default=LOCAL_USER, index=True)

    source_provider: Mapped[str] = mapped_column(String(32))
    source_playlist_id: Mapped[str] = mapped_column(String(191))
    target_provider: Mapped[str] = mapped_column(String(32))
    target_playlist_id: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(512), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class TransferJob(Base):
    __tablename__ = "transfer_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default=LOCAL_USER, index=True)

    source_provider: Mapped[str] = mapped_column(String(32))
    target_provider: Mapped[str] = mapped_column(String(32))
    source_playlist_id: Mapped[str] = mapped_column(String(191))
    source_playlist_name: Mapped[str] = mapped_column(String(512), default="")
    target_playlist_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    target_playlist_name: Mapped[str] = mapped_column(String(512), default="")

    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    items: Mapped[list["TransferItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="TransferItem.position"
    )

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.status] = out.get(item.status, 0) + 1
        return out


class TransferItem(Base):
    """One source track within one job. The unit of resumability."""

    __tablename__ = "transfer_items"
    __table_args__ = (
        UniqueConstraint("job_id", "source_track_id", "position", name="uq_job_track"),
        Index("ix_items_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("transfer_jobs.id"), index=True)
    job: Mapped[TransferJob] = relationship(back_populates="items")

    position: Mapped[int] = mapped_column(Integer, default=0)

    source_track_id: Mapped[str] = mapped_column(String(191))
    source_label: Mapped[str] = mapped_column(String(512), default="")
    # Stored as structured fields rather than reparsed from source_label: a
    # display string cannot be split back into title and artists reliably when
    # either contains the separator.
    source_title: Mapped[str] = mapped_column(String(512), default="")
    source_artists: Mapped[str] = mapped_column(Text, default="[]")
    source_album: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_isrc: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    target_track_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    target_label: Mapped[str] = mapped_column(String(512), default="")

    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default=ItemStatus.PENDING.value)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Serialized runners-up, so the review UI can offer alternatives without
    #: re-running search.
    candidates: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
