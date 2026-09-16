"""Add quality_score column to signal_decision_trace

Revision ID: b009
Revises: b008
Create Date: 2026-09-16

"Trade Quality Layer" v1 (external review round 2, Part 2): a 0-100
diagnostic score computed for every REJECTED/ENTERED candidate, recorded
alongside the existing gate-rejection detail. Observational only -- does not
gate any trade. signal_decision_trace itself predates Alembic tracking for
this project (created via create_all() as a brand-new table on 2026-09-15,
same as rejected_signal_outcome is this same session) -- this migration only
covers the new COLUMN on that already-live table.

New column (signal_decision_trace): quality_score INTEGER, nullable
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision: str = 'b009'
down_revision: Union[str, Sequence[str], None] = 'b008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_safe(table: str, column: sa.Column) -> None:
    """Add column, silently skip if it already exists (MySQL errno 1060)."""
    try:
        op.add_column(table, column)
    except OperationalError as e:
        if "1060" in str(e) or "Duplicate column" in str(e):
            pass  # column already present — nothing to do
        else:
            raise


def upgrade() -> None:
    _add_column_safe('signal_decision_trace', sa.Column('quality_score', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('signal_decision_trace', 'quality_score')
