"""Benchmark harness: ground-truth datasets, runs and per-item results.

* ``benchmark_datasets`` — workspace-scoped ground-truth datasets (kind: leads | email | enrichment).
* ``benchmark_items`` — one item per company (input given to the engine + expected values).
* ``benchmark_runs`` — a run of the engine against a dataset (mode registry | live) or a synthetic suite
  (mode suite), with measured metrics (value, n, Wilson 90 % interval), cost and duration.
* ``benchmark_results`` — per-item actual values and per-field verdicts (TP / FP / FN), latency, cost.

Revision ID: c7d2f8b3e1a4
Revises: b5c1e7a2d9f0
Create Date: 2026-10-06 07:10:30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c7d2f8b3e1a4'
down_revision: Union[str, None] = 'b5c1e7a2d9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=48)


def upgrade() -> None:
    op.create_table('benchmark_datasets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('kind', _enum('benchmark_dataset_kind', 'leads', 'email', 'enrichment'), server_default='leads', nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('item_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_benchmark_datasets_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_benchmark_datasets_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_benchmark_datasets'))
    )
    op.create_index(op.f('ix_benchmark_datasets_workspace_id'), 'benchmark_datasets', ['workspace_id'], unique=False)
    op.create_index('ix_benchmark_datasets_ws_created', 'benchmark_datasets', ['workspace_id', 'created_at'], unique=False)

    op.create_table('benchmark_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('input', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('expected', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['benchmark_datasets.id'], name=op.f('fk_benchmark_items_dataset_id_benchmark_datasets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_benchmark_items')),
    sa.UniqueConstraint('dataset_id', 'ordinal', name=op.f('uq_benchmark_items_dataset_id_ordinal'))
    )

    op.create_table('benchmark_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=True),
    sa.Column('suite_key', sa.Text(), nullable=True),
    sa.Column('mode', _enum('benchmark_mode', 'registry', 'live', 'suite'), nullable=False),
    sa.Column('strategy', sa.Text(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', _enum('benchmark_run_status', 'queued', 'running', 'completed', 'failed', 'cancelled'), server_default='queued', nullable=False),
    sa.Column('items_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('items_done', sa.Integer(), server_default='0', nullable=False),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('strategies', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('notes', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('job_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['benchmark_datasets.id'], name=op.f('fk_benchmark_runs_dataset_id_benchmark_datasets'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_benchmark_runs_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_benchmark_runs'))
    )
    op.create_index(op.f('ix_benchmark_runs_dataset_id'), 'benchmark_runs', ['dataset_id'], unique=False)
    op.create_index(op.f('ix_benchmark_runs_workspace_id'), 'benchmark_runs', ['workspace_id'], unique=False)
    op.create_index('ix_benchmark_runs_ws_created', 'benchmark_runs', ['workspace_id', 'created_at'], unique=False)

    op.create_table('benchmark_results',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('item_id', sa.UUID(), nullable=True),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('strategy', sa.Text(), nullable=True),
    sa.Column('expected', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('actual', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('verdicts', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('fp', sa.Integer(), server_default='0', nullable=False),
    sa.Column('fn', sa.Integer(), server_default='0', nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['benchmark_items.id'], name=op.f('fk_benchmark_results_item_id_benchmark_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['benchmark_runs.id'], name=op.f('fk_benchmark_results_run_id_benchmark_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_benchmark_results'))
    )
    op.create_index('ix_benchmark_results_run_ordinal', 'benchmark_results', ['run_id', 'ordinal'], unique=False)
    op.create_index('uq_benchmark_results_run_item', 'benchmark_results', ['run_id', 'item_id'], unique=True, postgresql_where=sa.text('item_id IS NOT NULL'))


def downgrade() -> None:
    # IF EXISTS: databases stamped at this revision while it was still an empty placeholder must be able
    # to downgrade too (dropping a table drops its indexes and constraints).
    for table in ('benchmark_results', 'benchmark_runs', 'benchmark_items', 'benchmark_datasets'):
        op.execute(sa.text(f'DROP TABLE IF EXISTS {table}'))
