"""Benchmark harness tables (docs/ARCHITECTURE.md §Benchmark Harness).

Ground-truth datasets (workspace-scoped), runs of the engine against them (registry / live / synthetic
suite) and per-item results with field-level verdicts. Kept out of ``models.py`` so the harness stays a
self-contained, removable module; it shares ``Base`` (same metadata, naming convention and helpers).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from scout.db.models import Base, created_at, enum_col, fk_uuid, pk, tstz, updated_at, ws_fk


class BenchmarkKind(StrEnum):
    leads = "leads"  # company → decision makers → emails (+ optional enrichment)
    email = "email"  # people given as input; expected addresses / statuses
    enrichment = "enrichment"  # company given; expected column values


class BenchmarkMode(StrEnum):
    registry = "registry"  # compare against what the workspace already holds (zero cost)
    live = "live"  # run the engine on each item (budget-capped)
    suite = "suite"  # pluggable synthetic suite (scout.benchmark.registry)


class BenchmarkRunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


BENCHMARK_RUN_TERMINAL = (
    BenchmarkRunStatus.completed,
    BenchmarkRunStatus.failed,
    BenchmarkRunStatus.cancelled,
)


class BenchmarkDataset(Base):
    __tablename__ = "benchmark_datasets"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[BenchmarkKind] = mapped_column(
        enum_col(BenchmarkKind, "benchmark_dataset_kind"), nullable=False, server_default="leads"
    )
    description: Mapped[str | None] = mapped_column(sa.Text)
    item_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # Import options and provenance: source format, column mapping, people_exhaustive, company_input,
    # enrichment column prompts ({key: {instruction, data_type}}), warnings.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_by: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (sa.Index("ix_benchmark_datasets_ws_created", "workspace_id", "created_at"),)


class BenchmarkItem(Base):
    __tablename__ = "benchmark_items"
    id: Mapped[uuid.UUID] = pk()
    dataset_id: Mapped[uuid.UUID] = fk_uuid("benchmark_datasets.id", ondelete="CASCADE", nullable=False)
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(sa.Text)
    # What the engine is given: {company: {domain?, name?, city?, country?}, people?: [{first, last, title?}]}
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Ground truth: {company: {domain?, name?, catch_all?, email_pattern?}, people: [{first, last, title?}],
    #  people_exhaustive: bool, emails: [{address?, status?, first?, last?}], enrichment: {column_key: value}}
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (sa.UniqueConstraint("dataset_id", "ordinal"),)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"
    id: Mapped[uuid.UUID] = pk()
    workspace_id: Mapped[uuid.UUID] = ws_fk()
    dataset_id: Mapped[uuid.UUID | None] = fk_uuid("benchmark_datasets.id", ondelete="SET NULL", index=True)
    suite_key: Mapped[str | None] = mapped_column(sa.Text)
    mode: Mapped[BenchmarkMode] = mapped_column(enum_col(BenchmarkMode, "benchmark_mode"), nullable=False)
    strategy: Mapped[str | None] = mapped_column(sa.Text)  # human label of the configuration
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[BenchmarkRunStatus] = mapped_column(
        enum_col(BenchmarkRunStatus, "benchmark_run_status"), nullable=False, server_default="queued"
    )
    items_total: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    items_done: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # {metric_key: {value, n, k, ci90, unit, label, group, definition}} — measured numbers only.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Suites: {strategy: {metric_key: {...}}} for side-by-side comparison.
    strategies: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error: Mapped[str | None] = mapped_column(sa.Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[datetime | None] = tstz()
    finished_at: Mapped[datetime | None] = tstz()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (sa.Index("ix_benchmark_runs_ws_created", "workspace_id", "created_at"),)


class BenchmarkResult(Base):
    __tablename__ = "benchmark_results"
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = fk_uuid("benchmark_runs.id", ondelete="CASCADE", nullable=False)
    item_id: Mapped[uuid.UUID | None] = fk_uuid("benchmark_items.id", ondelete="SET NULL")
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(sa.Text)
    strategy: Mapped[str | None] = mapped_column(sa.Text)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    actual: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Per field group: {company: {tp, fp, fn, …}, people: {…, pairs}, emails: {…, rows}, enrichment: {…}}
    verdicts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    fp: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    fn: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        sa.Index("ix_benchmark_results_run_ordinal", "run_id", "ordinal"),
        # Idempotent per-item writes: a resumed / retried run never stores an item twice.
        sa.Index(
            "uq_benchmark_results_run_item",
            "run_id",
            "item_id",
            unique=True,
            postgresql_where=sa.text("item_id IS NOT NULL"),
        ),
    )
