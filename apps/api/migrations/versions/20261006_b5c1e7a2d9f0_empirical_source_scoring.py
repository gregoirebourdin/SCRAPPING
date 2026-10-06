"""Empirical source scoring: generic resolver_stats (docs/ARCHITECTURE.md §Empirical Source Scoring).

* ``email_resolver_stats`` → ``resolver_stats`` (PK ``pk_email_resolver_stats`` → ``pk_resolver_stats``): the
  same counters now cover every resolver / source dimension (discovery sources, people sources, enrichment
  resolvers, search engines, crawl tiers) next to the email dimensions.
* ``successes`` — attempts that produced a usable result (coverage = successes / attempts). Legacy email
  rows only ever recorded attempts that produced a candidate, so they are backfilled with
  ``successes = attempts``.
* ``last_outcome_at`` — when the last ground-truth outcome (correct / wrong / inconclusive) arrived.

Revision ID: b5c1e7a2d9f0
Revises: 4678d41bd34a
Create Date: 2026-10-06 07:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5c1e7a2d9f0'
down_revision: Union[str, None] = '4678d41bd34a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("email_resolver_stats", "resolver_stats")
    # Renaming an index-backed constraint also renames its index.
    op.execute("ALTER TABLE resolver_stats RENAME CONSTRAINT pk_email_resolver_stats TO pk_resolver_stats")
    op.add_column(
        "resolver_stats",
        sa.Column("successes", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "resolver_stats",
        sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE resolver_stats
        SET successes = attempts,
            last_outcome_at = CASE
                WHEN confirmed_correct + confirmed_wrong + inconclusive > 0 THEN updated_at
            END
        """
    )


def downgrade() -> None:
    op.drop_column("resolver_stats", "last_outcome_at")
    op.drop_column("resolver_stats", "successes")
    op.execute("ALTER TABLE resolver_stats RENAME CONSTRAINT pk_resolver_stats TO pk_email_resolver_stats")
    op.rename_table("resolver_stats", "email_resolver_stats")
