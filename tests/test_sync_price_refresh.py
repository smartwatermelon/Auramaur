"""Tests for portfolio price-refresh from exchange API.

Verifies that PositionSyncer refreshes stale current_price values
using the discovery client (MarketDiscovery.get_market), and that
TradingEngine.scan_and_store_markets refreshes prices for held
positions not covered by the regular scan.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from auramaur.broker.sync import PositionSyncer
from auramaur.exchange.models import (
    Market,
    OrderSide,
    Position,
    TokenType,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.is_live = False
    return s


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.fetchall = AsyncMock(return_value=[])
    db.fetchone = AsyncMock(return_value=None)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def mock_exchange():
    return MagicMock()


@pytest.fixture
def mock_pnl():
    pnl = AsyncMock()
    pnl.get_cost_basis = AsyncMock(return_value=(0.45, 10.0))
    pnl.get_token_info = AsyncMock(return_value=(TokenType.YES, "token-abc"))
    return pnl


@pytest.fixture
def mock_paper():
    paper = MagicMock()
    paper.balance = 500.0
    paper.positions = {
        "market-1": Position(
            market_id="market-1",
            side=OrderSide.BUY,
            size=10,
            avg_price=0.45,
            current_price=0.45,
            category="politics",
            token=TokenType.YES,
            token_id="token-abc",
        ),
    }
    return paper


@pytest.fixture
def mock_discovery():
    discovery = AsyncMock()
    discovery.get_market = AsyncMock(
        return_value=Market(
            id="market-1",
            question="Test?",
            outcome_yes_price=0.72,
            outcome_no_price=0.28,
        )
    )
    return discovery


# ── Part A: PositionSyncer price refresh ──────────────────────────


@pytest.mark.asyncio
async def test_paper_sync_refreshes_price(
    mock_settings,
    mock_db,
    mock_exchange,
    mock_paper,
    mock_pnl,
    mock_discovery,
):
    """Discovery returns fresh price; syncer should update current_price."""
    syncer = PositionSyncer(
        settings=mock_settings,
        db=mock_db,
        exchange=mock_exchange,
        paper=mock_paper,
        pnl=mock_pnl,
        discovery=mock_discovery,
    )

    positions = await syncer._sync_paper()

    assert len(positions) == 1
    pos = positions[0]
    assert pos.market_id == "market-1"
    # Price should be refreshed from discovery (YES token → yes_price = 0.72)
    assert pos.current_price == pytest.approx(0.72)
    mock_discovery.get_market.assert_awaited_once_with("market-1")


@pytest.mark.asyncio
async def test_paper_sync_refreshes_no_token_price(
    mock_settings,
    mock_db,
    mock_exchange,
    mock_pnl,
):
    """For NO tokens, syncer should use outcome_no_price from discovery."""
    paper = MagicMock()
    paper.balance = 500.0
    paper.positions = {
        "market-2": Position(
            market_id="market-2",
            side=OrderSide.BUY,
            size=5,
            avg_price=0.30,
            current_price=0.30,
            category="crypto",
            token=TokenType.NO,
            token_id="token-no-xyz",
        ),
    }

    pnl = AsyncMock()
    pnl.get_cost_basis = AsyncMock(return_value=(0.30, 5.0))
    pnl.get_token_info = AsyncMock(return_value=(TokenType.NO, "token-no-xyz"))

    discovery = AsyncMock()
    discovery.get_market = AsyncMock(
        return_value=Market(
            id="market-2",
            question="Crypto?",
            outcome_yes_price=0.40,
            outcome_no_price=0.60,
        )
    )

    syncer = PositionSyncer(
        settings=mock_settings,
        db=mock_db,
        exchange=MagicMock(),
        paper=paper,
        pnl=pnl,
        discovery=discovery,
    )

    positions = await syncer._sync_paper()
    assert positions[0].current_price == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_sync_falls_back_on_api_error(
    mock_settings,
    mock_db,
    mock_exchange,
    mock_paper,
    mock_pnl,
):
    """If discovery.get_market raises, sync should still return positions
    with the stale price — graceful fallback, no crash."""
    discovery = AsyncMock()
    discovery.get_market = AsyncMock(side_effect=Exception("API timeout"))

    syncer = PositionSyncer(
        settings=mock_settings,
        db=mock_db,
        exchange=mock_exchange,
        paper=mock_paper,
        pnl=mock_pnl,
        discovery=discovery,
    )

    positions = await syncer._sync_paper()

    assert len(positions) == 1
    # Price stays at the stale value from PaperTrader
    assert positions[0].current_price == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_sync_skips_zero_prices(
    mock_settings,
    mock_db,
    mock_exchange,
    mock_paper,
    mock_pnl,
):
    """If discovery returns zero price, keep the stale price."""
    discovery = AsyncMock()
    discovery.get_market = AsyncMock(
        return_value=Market(
            id="market-1",
            question="Test?",
            outcome_yes_price=0.0,
            outcome_no_price=0.0,
        )
    )

    syncer = PositionSyncer(
        settings=mock_settings,
        db=mock_db,
        exchange=mock_exchange,
        paper=mock_paper,
        pnl=mock_pnl,
        discovery=discovery,
    )

    positions = await syncer._sync_paper()
    # Both prices zero → skip refresh, keep stale price
    assert positions[0].current_price == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_sync_no_discovery_skips_refresh(
    mock_settings,
    mock_db,
    mock_exchange,
    mock_paper,
    mock_pnl,
):
    """Without a discovery client, syncer should behave as before."""
    syncer = PositionSyncer(
        settings=mock_settings,
        db=mock_db,
        exchange=mock_exchange,
        paper=mock_paper,
        pnl=mock_pnl,
    )

    positions = await syncer._sync_paper()
    assert len(positions) == 1
    assert positions[0].current_price == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_live_sync_refreshes_price(mock_db, mock_pnl):
    """Live sync should also refresh prices from discovery."""
    settings = MagicMock()
    settings.is_live = True

    mock_db.fetchall = AsyncMock(
        return_value=[
            {
                "market_id": "market-live-1",
                "token": "YES",
                "token_id": "tok-1",
                "size": 20.0,
                "avg_cost": 0.50,
                "outcome_yes_price": 0.50,
                "outcome_no_price": 0.50,
                "category": "politics",
            },
        ]
    )

    discovery = AsyncMock()
    discovery.get_market = AsyncMock(
        return_value=Market(
            id="market-live-1",
            question="Live?",
            outcome_yes_price=0.85,
            outcome_no_price=0.15,
        )
    )

    syncer = PositionSyncer(
        settings=settings,
        db=mock_db,
        exchange=MagicMock(),
        paper=MagicMock(),
        pnl=mock_pnl,
        discovery=discovery,
    )

    positions = await syncer._sync_live()
    assert len(positions) == 1
    assert positions[0].current_price == pytest.approx(0.85)


# ── Part C: Engine held-position refresh ──────────────────────────


@pytest.mark.asyncio
async def test_scan_refreshes_held_positions():
    """scan_and_store_markets should refresh prices for held positions
    that weren't in the regular scan results."""
    from auramaur.strategy.engine import TradingEngine

    # 3 markets from the regular scan
    scanned = [
        Market(
            id="scan-1", question="Q1?", outcome_yes_price=0.60, outcome_no_price=0.40
        ),
        Market(
            id="scan-2", question="Q2?", outcome_yes_price=0.70, outcome_no_price=0.30
        ),
        Market(
            id="scan-3", question="Q3?", outcome_yes_price=0.55, outcome_no_price=0.45
        ),
    ]

    # 2 held positions NOT in the scan
    held_rows = [
        {"market_id": "held-1"},
        {"market_id": "held-2"},
    ]

    held_market_1 = Market(
        id="held-1",
        question="Held1?",
        outcome_yes_price=0.95,
        outcome_no_price=0.05,
    )
    held_market_2 = Market(
        id="held-2",
        question="Held2?",
        outcome_yes_price=0.88,
        outcome_no_price=0.12,
    )

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    async def mock_fetchall(query, params=None):
        if "portfolio" in query and "market_id" in query:
            return held_rows
        return []

    db.fetchall = AsyncMock(side_effect=mock_fetchall)

    discovery = AsyncMock()
    discovery.get_markets = AsyncMock(return_value=scanned)

    async def mock_get_market(mid):
        if mid == "held-1":
            return held_market_1
        if mid == "held-2":
            return held_market_2
        return None

    discovery.get_market = AsyncMock(side_effect=mock_get_market)

    engine = TradingEngine(
        settings=MagicMock(),
        db=db,
        discovery=discovery,
        aggregator=MagicMock(),
        analyzer=MagicMock(),
        cache=MagicMock(),
        risk_manager=MagicMock(),
        exchange=MagicMock(),
    )
    engine.exchange_name = "polymarket"

    markets = await engine.scan_and_store_markets()

    # Regular scan should return original 3 markets
    assert len(markets) == 3

    # discovery.get_market should have been called for both held positions
    get_market_calls = [c.args[0] for c in discovery.get_market.call_args_list]
    assert "held-1" in get_market_calls
    assert "held-2" in get_market_calls

    # DB execute should include UPDATE calls for held positions
    update_calls = [
        c
        for c in db.execute.call_args_list
        if c.args and isinstance(c.args[0], str) and "UPDATE markets" in c.args[0]
    ]
    assert len(update_calls) == 2

    # SQL params: (yes_price, no_price, last_updated, market_id)
    for call in update_calls:
        assert len(call.args[1]) == 4
    update_params = {call.args[1][3]: call.args[1][:2] for call in update_calls}
    assert update_params["held-1"] == (0.95, 0.05)
    assert update_params["held-2"] == (0.88, 0.12)


@pytest.mark.asyncio
async def test_scan_held_refresh_tolerates_errors():
    """If get_market fails for a held position, the scan should still complete."""
    from auramaur.strategy.engine import TradingEngine

    scanned = [
        Market(
            id="scan-1", question="Q?", outcome_yes_price=0.60, outcome_no_price=0.40
        ),
    ]
    held_rows = [{"market_id": "held-err"}]

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    async def mock_fetchall(query, params=None):
        if "portfolio" in query and "market_id" in query:
            return held_rows
        return []

    db.fetchall = AsyncMock(side_effect=mock_fetchall)

    discovery = AsyncMock()
    discovery.get_markets = AsyncMock(return_value=scanned)
    discovery.get_market = AsyncMock(side_effect=Exception("Gamma 500"))

    engine = TradingEngine(
        settings=MagicMock(),
        db=db,
        discovery=discovery,
        aggregator=MagicMock(),
        analyzer=MagicMock(),
        cache=MagicMock(),
        risk_manager=MagicMock(),
        exchange=MagicMock(),
    )
    engine.exchange_name = "polymarket"

    # Should not raise
    markets = await engine.scan_and_store_markets()
    assert len(markets) == 1
