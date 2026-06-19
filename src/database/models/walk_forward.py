from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Float, Integer, JSON, text, Index
from src.database.base import Base


class WalkForwardResult(Base):
    """
    Stores results from one window of a walk-forward test.

    Walk-forward test structure:
      Train window: [window_start → train_end)  (typically 2 years)
      Test  window: [train_end   → window_end)  (typically 1 year)

    The 'is_oos' flag distinguishes in-sample (train) vs out-of-sample (test) results.
    If OOS profit_factor consistently mirrors IS profit_factor, the strategy is robust.
    If they diverge heavily, it is curve-fit.
    """
    __tablename__ = "walk_forward_results"
    __table_args__ = (
        Index("idx_wf_strategy",  "strategy_name"),
        Index("idx_wf_window",    "window_start", "window_end"),
    )

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name  = Column(String(50),  nullable=False)
    symbol         = Column(String(30),  nullable=True)   # None = portfolio-level

    # Window boundaries (ISO date strings — avoids TZ complexity)
    window_start   = Column(String(20),  nullable=False)   # e.g. "2020-01-01"
    train_end      = Column(String(20),  nullable=False)   # e.g. "2022-01-01"
    window_end     = Column(String(20),  nullable=False)   # e.g. "2023-01-01"
    is_oos         = Column(Integer,     nullable=False, default=0)  # 0=in-sample, 1=oos

    # Core metrics
    profit_factor  = Column(Float, nullable=True)
    sharpe_ratio   = Column(Float, nullable=True)
    max_drawdown   = Column(Float, nullable=True)   # percentage
    win_rate       = Column(Float, nullable=True)   # 0.0–1.0
    total_pnl      = Column(Float, nullable=True)   # ₹
    trade_count    = Column(Integer, nullable=True)
    avg_pnl        = Column(Float, nullable=True)
    expectancy     = Column(Float, nullable=True)   # avg_win × win_rate - avg_loss × loss_rate

    # Parameters used for this window
    parameters     = Column(JSON, nullable=True)   # {"fast": 20, "slow": 50, ...}

    run_at         = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
