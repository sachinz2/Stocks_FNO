from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Integer, Index
from src.database.base import Base


class GateAuditSnapshot(Base):
    """
    Periodic snapshot of per-strategy entry-gate pass counts -- see
    LiveTradingEngine._audit_gate()/_flush_gate_audit_snapshot().

    Added 2026-09-03 (external review): _signal_gate_stats already tracked
    this in memory, but reset on every restart -- every "why no trades"
    investigation this session (VIX gate killing credit_spread_v1/
    iron_condor_v1, RVOL corruption starving momentum_v1) had to be
    answered by ad-hoc log-grepping instead of a query. One row per
    (strategy, gate) each time a snapshot is taken (once per signal cycle,
    not per candidate -- see _flush_gate_audit_snapshot()'s docstring for
    why write volume is bounded this way).

    Counts are CUMULATIVE for the trading day (reset at market open, same
    as _signal_gate_stats itself), not per-cycle deltas -- a gate whose
    count stops growing while earlier gates (pool/signal_generated) keep
    growing is the live bottleneck for that strategy. Compute deltas
    between consecutive snapshot_time rows at query time if a per-cycle
    rate is needed.
    """
    __tablename__ = "gate_audit_snapshot"
    __table_args__ = (
        Index("idx_gas_strategy_gate_time", "strategy_name", "gate", "snapshot_time"),
    )

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_time  = Column(TIMESTAMP, nullable=False)
    strategy_name  = Column(String(50), nullable=False)
    gate           = Column(String(50), nullable=False)
    pass_count     = Column(Integer, nullable=False)
