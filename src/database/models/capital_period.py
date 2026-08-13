from sqlalchemy import Column, BigInteger, Date, Numeric, Boolean, TIMESTAMP, Index, text
from src.database.base import Base


class CapitalPeriod(Base):
    """
    One expiry-to-expiry trading "month" for capital compounding.

    period_start/period_end bound an NSE monthly F&O expiry cycle (see
    core/utils.get_capital_period_bounds -- reuses the same
    _last_expiry_weekday() the strategies already use for DTE/rollover
    logic). starting_capital is fixed for the whole period; realized_pnl/
    ending_capital are only set once the period closes (see
    src/portfolio/capital_periods.py's rollover job), at which point
    ending_capital becomes the next period's starting_capital --
    profits/losses compound month over month instead of every period
    starting from the same static INITIAL_CAPITAL.
    """
    __tablename__ = "capital_periods"
    __table_args__ = (
        Index("idx_cp_period_start", "period_start"),
        Index("idx_cp_active", "closed"),
    )

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    period_start     = Column(Date, nullable=False)
    period_end       = Column(Date, nullable=False)  # the expiry date this period closes on
    starting_capital = Column(Numeric(18, 4), nullable=False)
    realized_pnl     = Column(Numeric(18, 4), nullable=True)   # set when closed
    ending_capital   = Column(Numeric(18, 4), nullable=True)   # set when closed
    closed           = Column(Boolean, nullable=False, server_default=text("0"))

    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
