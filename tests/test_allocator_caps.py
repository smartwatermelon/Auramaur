"""Tests for CapitalAllocator per-cycle deployment cap and cash reserve floor."""

from __future__ import annotations

from unittest.mock import MagicMock

import structlog.testing

from auramaur.broker.allocator import CapitalAllocator, CandidateTrade
from auramaur.exchange.models import Confidence, Market, Signal
from auramaur.risk.manager import RiskDecision


def make_settings(reserve_pct: float = 0.0, cycle_pct: float = 0.0):
    s = MagicMock()
    s.risk.max_open_positions = 500
    s.risk.category_exposure_cap_pct = 60.0
    s.risk.cash_reserve_floor_pct = reserve_pct
    s.risk.per_cycle_deploy_pct = cycle_pct
    return s


def make_candidate(
    market_id: str, kelly_size: float = 25.0, category: str = "politics"
):
    market = MagicMock(spec=Market)
    market.id = market_id
    market.category = category
    signal = MagicMock(spec=Signal)
    signal.edge = 10.0
    signal.claude_confidence = Confidence.HIGH
    decision = MagicMock(spec=RiskDecision)
    return CandidateTrade(
        market=market,
        signal=signal,
        risk_decision=decision,
        kelly_size=kelly_size,
        expected_value=0.1 * kelly_size,
    )


def test_cash_reserve_floor():
    """Reserve=20%, equity=$500 all cash. Total deployable <= $400."""
    allocator = CapitalAllocator(make_settings(reserve_pct=20.0))
    candidates = [
        make_candidate(f"mkt_{i}") for i in range(5)
    ]  # 5 × $25 = $125 desired
    result = allocator.allocate(
        candidates, available_capital=500.0, current_positions=[]
    )
    total_allocated = sum(c.allocated_size for c in result)
    # With $500 equity and 20% reserve, max deployable = $400
    # But only $125 is desired, so all should be allocated (125 <= 400)
    assert total_allocated <= 400.0, f"Expected <= $400, got ${total_allocated}"
    # Also verify all 5 candidates got allocated (budget is not binding here)
    assert len(result) == 5


def test_per_cycle_cap():
    """Cap=15%, equity=$500 all cash. Total deployed per cycle <= $75."""
    allocator = CapitalAllocator(make_settings(cycle_pct=15.0))
    candidates = [
        make_candidate(f"mkt_{i}") for i in range(5)
    ]  # 5 × $25 = $125 desired
    result = allocator.allocate(
        candidates, available_capital=500.0, current_positions=[]
    )
    total_allocated = sum(c.allocated_size for c in result)
    # Cycle cap = 15% × $500 = $75
    assert total_allocated <= 75.0, f"Expected <= $75, got ${total_allocated}"


def test_both_constraints():
    """Reserve=20%, cap=15%, cash=$400, positions=$600 (equity=$1000).

    Reserve: keep $200 (20% × $1000), so deployable cash = $400 - $200 = $200.
    Cycle cap: $1000 × 15% = $150.
    Binding constraint: $150.
    """
    positions = [
        MagicMock(market_id=f"pos_{i}", size=120, current_price=1.0, avg_cost=1.0)
        for i in range(5)
    ]
    allocator = CapitalAllocator(make_settings(reserve_pct=20.0, cycle_pct=15.0))
    candidates = [
        make_candidate(f"mkt_{i}") for i in range(10)
    ]  # 10 × $25 = $250 desired
    result = allocator.allocate(
        candidates, available_capital=400.0, current_positions=positions
    )
    total_allocated = sum(c.allocated_size for c in result)
    # Binding constraint is $150
    assert total_allocated <= 150.0, f"Expected <= $150, got ${total_allocated}"


def test_zero_reserve_disables():
    """reserve=0 means no floor — all candidates allocate normally."""
    allocator = CapitalAllocator(make_settings(reserve_pct=0.0, cycle_pct=0.0))
    candidates = [
        make_candidate(f"mkt_{i}") for i in range(5)
    ]  # 5 × $25 = $125 desired
    result = allocator.allocate(
        candidates, available_capital=500.0, current_positions=[]
    )
    total_allocated = sum(c.allocated_size for c in result)
    # No constraints — all $125 should be allocated
    assert total_allocated == 125.0, f"Expected $125, got ${total_allocated}"
    assert len(result) == 5


def test_zero_cap_disables():
    """cycle_pct=0 means no per-cycle cap — all candidates allocate normally."""
    allocator = CapitalAllocator(make_settings(reserve_pct=0.0, cycle_pct=0.0))
    candidates = [
        make_candidate(f"mkt_{i}") for i in range(5)
    ]  # 5 × $25 = $125 desired
    result = allocator.allocate(
        candidates, available_capital=500.0, current_positions=[]
    )
    total_allocated = sum(c.allocated_size for c in result)
    # No constraints — all $125 should be allocated
    assert total_allocated == 125.0, f"Expected $125, got ${total_allocated}"
    assert len(result) == 5


def test_allocator_logs_constraints():
    """Verify that an allocator.capital_constraints event is emitted with correct fields."""
    allocator = CapitalAllocator(make_settings(reserve_pct=20.0, cycle_pct=15.0))
    candidates = [make_candidate(f"mkt_{i}") for i in range(3)]

    with structlog.testing.capture_logs() as cap:
        allocator.allocate(candidates, available_capital=500.0, current_positions=[])

    constraint_logs = [
        e for e in cap if e.get("event") == "allocator.capital_constraints"
    ]
    assert len(constraint_logs) == 1, (
        f"Expected exactly one 'allocator.capital_constraints' event, got: "
        f"{[e.get('event') for e in cap]}"
    )
    entry = constraint_logs[0]
    # equity = available (no positions) = $500
    assert entry["equity"] == 500.0
    assert entry["available_cash"] == 500.0
    # reserve_floor = 20% × $500 = $100
    assert entry["reserve_floor"] == 100.0
    # cycle_cap = 15% × $500 = $75 (binding)
    assert entry["cycle_cap"] == 75.0
    # effective_budget = min($500 - $100, $75) = $75
    assert entry["effective_budget"] == 75.0
