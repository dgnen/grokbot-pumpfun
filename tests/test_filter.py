"""The monitor's base filter: it cuts 94% of the flow, so boundary
values matter more than anything else.
"""

import asyncio
import json
import time

import pytest

from src.models import Config, FilterConfig, Token
from src.monitor import CURVE_COMPLETION_SOL, LaunchMonitor, parse_create_event, passes_filter


@pytest.fixture
def cfg() -> FilterConfig:
    return FilterConfig(
        min_unique_buyers=5,
        max_curve_progress=0.40,
        require_metadata=True,
        min_age_seconds=120.0,
    )


def make_token(**overrides) -> Token:
    base = {
        "mint": "Mint111",
        "name": "Doge Killer",
        "image_uri": "https://img/1.png",
        "created_timestamp": time.time() - 300,
        "unique_buyers": 10,
        "curve_progress": 0.15,
    }
    base.update(overrides)
    return Token(**base)


def test_healthy_token_passes(cfg):
    ok, reason = passes_filter(make_token(), cfg)
    assert ok and reason == "ok"


def test_missing_metadata_rejected(cfg):
    ok, reason = passes_filter(make_token(image_uri=None), cfg)
    assert not ok and reason == "no_metadata"

    ok, reason = passes_filter(make_token(name=None), cfg)
    assert not ok and reason == "no_metadata"


def test_metadata_ignored_when_not_required(cfg):
    cfg.require_metadata = False
    ok, _ = passes_filter(make_token(image_uri=None), cfg)
    assert ok


def test_too_young_rejected(cfg):
    ok, reason = passes_filter(make_token(created_timestamp=time.time() - 60), cfg)
    assert not ok and reason == "too_young"


def test_age_boundary_is_inclusive(cfg):
    """Exactly 120 seconds already passes; 119.9 does not."""
    ok, _ = passes_filter(make_token(created_timestamp=time.time() - 120.5), cfg)
    assert ok
    ok, reason = passes_filter(make_token(created_timestamp=time.time() - 119.0), cfg)
    assert not ok and reason == "too_young"


def test_buyers_boundary(cfg):
    ok, _ = passes_filter(make_token(unique_buyers=5), cfg)
    assert ok
    ok, reason = passes_filter(make_token(unique_buyers=4), cfg)
    assert not ok and reason == "few_buyers"


def test_curve_boundary(cfg):
    ok, _ = passes_filter(make_token(curve_progress=0.399), cfg)
    assert ok
    ok, reason = passes_filter(make_token(curve_progress=0.40), cfg)
    assert not ok and reason == "curve_too_full"


def test_terminal_reasons_win_over_temporary(cfg):
    """Reason order matters: a hopeless token must not sit in the buffer
    as 'too_young' or 'few_buyers' — otherwise it stays there until it goes stale."""
    token = make_token(image_uri=None, created_timestamp=time.time())
    ok, reason = passes_filter(token, cfg)
    assert not ok and reason == "no_metadata"

    token = make_token(curve_progress=0.9, unique_buyers=0, created_timestamp=time.time())
    ok, reason = passes_filter(token, cfg)
    assert not ok and reason == "curve_too_full"


# --- parsing a socket event -----------------------------------------------


def test_parse_create_event():
    token = parse_create_event(
        {
            "txType": "create",
            "mint": "Abc",
            "name": "Cat",
            "symbol": "CAT",
            "image": "https://img",
            "traderPublicKey": "Creator1",
            "vSolInBondingCurve": 38.5,
            "marketCapSol": 30.0,
        }
    )
    assert token is not None
    assert token.mint == "Abc"
    # 30 SOL in the reserve are virtual: 8.5 was actually raised
    assert token.curve_progress == pytest.approx(8.5 / CURVE_COMPLETION_SOL)


def test_fresh_launch_has_zero_progress():
    """A newborn curve's reserve is 30 virtual SOL. If you count them as
    progress, every launch is born at 35% and the curve filter becomes
    several times stricter than intended."""
    token = parse_create_event({
        "txType": "create", "mint": "New", "name": "n", "image": "i",
        "vSolInBondingCurve": 30.0,
    })
    assert token is not None
    assert token.curve_progress == 0.0


def test_parse_ignores_trades():
    assert parse_create_event({"txType": "buy", "mint": "Abc"}) is None
    assert parse_create_event({"txType": "create"}) is None


# --- monitor buffer -------------------------------------------------------


def make_monitor(skips: list) -> LaunchMonitor:
    config = Config()
    config.filter = FilterConfig(min_unique_buyers=3, min_age_seconds=120.0)
    return LaunchMonitor(config, on_skip=lambda t, r: skips.append((t.mint, r)))


def test_new_launch_is_buffered_not_emitted():
    skips: list = []
    mon = make_monitor(skips)
    assert mon.handle_event({"txType": "create", "mint": "A", "name": "n", "image": "i"}) is None
    assert "A" in mon.pending


def test_token_emitted_once_it_matures():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {
            "txType": "create",
            "mint": "A",
            "name": "n",
            "image": "i",
            "traderPublicKey": "creator",
            "timestamp": (time.time() - 300) * 1000,
        }
    )
    for wallet in ("w1", "w2", "w3"):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    assert out is not None  # emitted exactly on the third unique buyer
    assert out.mint == "A"
    assert "A" not in mon.pending
    assert skips == []


def test_same_wallet_does_not_inflate_buyer_count():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    for _ in range(10):
        out = mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": "same"})
    assert out is None
    assert mon.pending["A"].unique_buyers == 1


def test_curve_overflow_skips_permanently():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    mon.handle_event(
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1",
         "vSolInBondingCurve": CURVE_COMPLETION_SOL * 0.9}
    )
    assert "A" not in mon.pending
    assert skips == [("A", "curve_too_full")]


def test_sweep_drops_stale_launches():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 5000) * 1000}
    )
    ready = mon.sweep()
    assert ready == []
    assert skips == [("A", "stale_no_traction")]
    assert mon.pending == {}


def test_sweep_emits_matured_token():
    skips: list = []
    mon = make_monitor(skips)
    mon.handle_event(
        {"txType": "create", "mint": "A", "name": "n", "image": "i",
         "timestamp": (time.time() - 300) * 1000}
    )
    for wallet in ("w1", "w2", "w3", "w4"):
        mon.handle_event({"txType": "buy", "mint": "A", "traderPublicKey": wallet})
    # already emitted on the last buy, not emitted again
    assert mon.sweep() == []


# --- monitor memory -------------------------------------------------------


def test_seen_set_forgets_oldest():
    """The process lives for days: the seen-mint list must not grow forever."""
    from src.monitor import SeenSet

    seen = SeenSet(maxlen=3)
    for mint in ("A", "B", "C", "D"):
        seen.add(mint)
    assert len(seen) == 3
    assert "A" not in seen
    assert "D" in seen


def test_pending_buffer_is_bounded(monkeypatch):
    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "MAX_PENDING", 3)
    skips: list = []
    mon = make_monitor(skips)
    for index in range(5):
        mon.handle_event({
            "txType": "create", "mint": f"M{index}", "name": "n", "image": "i",
            "timestamp": (time.time() - 1000 + index) * 1000,
        })
    assert len(mon.pending) <= 3
    assert skips and skips[0][1] == "buffer_overflow"


# --- socket stream --------------------------------------------------------


class FakeWebSocket:
    """A socket that yields prepared messages, then goes silent."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = [json.dumps(m) for m in messages]
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(3600)          # then silence
        raise AssertionError("unreachable")

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def patch_socket(monkeypatch, sockets: list) -> list:
    """Replace websockets.connect with a sequence of sockets."""
    import src.monitor as monitor_module

    opened: list = []

    def connect(url, **kwargs):
        opened.append(url)
        socket = sockets.pop(0)
        if isinstance(socket, Exception):
            raise socket
        return socket

    monkeypatch.setattr(monitor_module.websockets, "connect", connect)
    return opened


async def test_stream_yields_matured_token(monkeypatch):
    created = (time.time() - 300) * 1000
    ws = FakeWebSocket([
        {"txType": "create", "mint": "A", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "A", "traderPublicKey": "w3"},
    ])
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "A"
    methods = [m["method"] for m in ws.sent]
    assert methods[0] == "subscribeNewToken"
    assert "subscribeTokenTrade" in methods       # subscribed to the launch's trades
    assert "unsubscribeTokenTrade" in methods     # and unsubscribed when we emitted it


async def test_stream_reconnects_after_drop(monkeypatch):
    created = (time.time() - 300) * 1000
    good = FakeWebSocket([
        {"txType": "create", "mint": "B", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "B", "traderPublicKey": "w3"},
    ])
    opened = patch_socket(monkeypatch, [OSError("socket dropped"), good])
    real_sleep = asyncio.sleep
    # the reconnect pause is needed in prod, not in the test
    monkeypatch.setattr(asyncio, "sleep", lambda delay, *a, **k: real_sleep(0))

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()

    assert token.mint == "B"
    assert len(opened) == 2          # the first connect failed, the second one worked


async def test_stream_survives_broken_json(monkeypatch):
    created = (time.time() - 300) * 1000

    class NoisyWebSocket(FakeWebSocket):
        async def recv(self) -> str:
            if self.messages:
                return self.messages.pop(0)
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    ws = NoisyWebSocket([
        {"txType": "create", "mint": "C", "name": "Cat", "image": "i", "timestamp": created},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w1"},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w2"},
        {"txType": "buy", "mint": "C", "traderPublicKey": "w3"},
    ])
    ws.messages.insert(1, "{broken json")
    patch_socket(monkeypatch, [ws])

    mon = make_monitor([])
    stream = mon.stream()
    token = await asyncio.wait_for(stream.__anext__(), timeout=2)
    await stream.aclose()
    assert token.mint == "C"
