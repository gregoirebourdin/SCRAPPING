"""schema follow ups

* ``ix_emails_address`` — global lookups by address (pattern-memory idempotence, identity by email).
* ``ix_grounded_research_ws_key_created`` — grounded-research cache lookup (workspace, key, freshness).
* ``domain_dns_cache.null_mx`` — RFC 7505 null MX as a real column (was encoded in ``error``); backfilled.
* ``companies.last_tech_scan_at`` — last technology scan, so zero-tech sites are not re-scanned within the
  freshness window; backfilled from existing ``technologies`` rows.

Revision ID: 7f6a512b9445
Revises: ec5a2c19301b
Create Date: 2026-10-06 01:06:50.915275

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f6a512b9445'
down_revision: Union[str, Sequence[str], None] = 'ec5a2c19301b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('companies', sa.Column('last_tech_scan_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('domain_dns_cache', sa.Column('null_mx', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_index('ix_emails_address', 'emails', ['address'], unique=False)
    op.create_index('ix_grounded_research_ws_key_created', 'grounded_research', ['workspace_id', 'cache_key', 'created_at'], unique=False)
    # backfills
    op.execute("UPDATE domain_dns_cache SET null_mx = true WHERE error = 'null_mx'")
    op.execute(
        "UPDATE companies c SET last_tech_scan_at = t.scanned_at "
        "FROM (SELECT company_id, max(observed_at) AS scanned_at FROM technologies GROUP BY company_id) t "
        "WHERE t.company_id = c.id"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_grounded_research_ws_key_created', table_name='grounded_research')
    op.drop_index('ix_emails_address', table_name='emails')
    op.drop_column('domain_dns_cache', 'null_mx')
    op.drop_column('companies', 'last_tech_scan_at')
