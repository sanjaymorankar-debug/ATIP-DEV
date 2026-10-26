"""
Transaction costs (BT-05): one itemised model, configured in one place.

Every backtest leg is charged through CostModel.charges(side, value). The
engines before W2 each carried a blended round-trip constant (0.25%), so a
cost assumption could only be changed by editing strategy code.

Presets (rates as published for NSE cash equities as understood in 2026-09 --
they change; check them against a recent contract note from your broker):

  nse_delivery   CNC: brokerage 0 (Dhan delivery), STT 0.1% on buy and sell,
                 NSE transaction charge 0.00297%, SEBI fee 0.0001% (Rs 10/crore),
                 stamp duty 0.015% on buy, GST 18% on brokerage + exchange + SEBI,
                 and DP_CHARGE_PER_SELL on every sell (below).
  nse_intraday   MIS: brokerage 0.03% capped at Rs 20 per order, STT 0.025% on
                 sell, stamp duty 0.003% on buy, other rates as above.
  flat           a single % per leg (the old blended assumption, for comparison)
  zero           no costs (to measure gross edge; never the default)

Configuration (atip_data/config.json), all optional:

    "backtest": {
      "cost_model": "nse_delivery",
      "cost_overrides": {"dp_charge_per_sell": 15.93},
      "futures": {"cost_model": "nse_futures", "cost_overrides": {}}
    }

ONE MODEL FOR PAPER TOO (P1-5): orders/paper.py charges every paper fill through the model
configured here (backtest.cost_model + cost_overrides; config.json paper_cost_model overrides it
for paper only). The paper book used to charge a flat 0.05% a leg -- less than half of the
statutory 0.22% round trip, and no DP charge.

DP CHARGE: nse_delivery charges DP_CHARGE_PER_SELL (Rs 15.93, GST included) on every sell, the
figure the 2026-10-26 feasibility study's backtests used; it was 0. Dhan's published rate
appears to be Rs 12.50 + GST (Rs 14.75) per scrip per day of sale -- check a contract note and
set "backtest": {"cost_overrides": {"dp_charge_per_sell": ...}}, which paper fills follow too.
The depository charges once per scrip per day; here every sell order pays it (two sells of one
stock on one day pay twice, slightly pessimistic).

ROUND TRIP (P1-5): the quick harnesses (scores/backtest.py, strategy/backtest_history.py,
strategy/backtest_aggressive.py, research/exit_target_study.py) charge one blended round trip,
ROUND_TRIP_PCT (0.25%), through round_trip_cost(): half of it on the entry leg's value, half on
the exit leg's. strategy/aggressive.py's realised_pnl used to charge the whole 0.25% on BOTH
legs (~0.5% a round trip) while scores/backtest.py charged it once. The blend is below this
itemised model: statutory ~0.22% + 2 x 5 bps slippage + the DP charge is ~0.32% + Rs 15.93.

OPTIONS (P1-5, execution/options_paper.py): OptionsCostModel, preset nse_options, charged on the
premium (value = quantity x premium); NSE equity and index options as of 2026-10 (check a
contract note):

    STT            0.15% of the SELL premium        Finance Act 2026, from 2026-04-01 (0.1% before)
                   0.15% of the intrinsic value     a long option exercised at expiry (0.125%
                                                    before 2026-04-01); none on an assigned short
    exchange       0.03503% of premium              NSE options transaction charge, Rs 3,503 /
                                                    crore of premium from 2024-10-01
    SEBI fee       0.0001% of premium (Rs 10 / crore)
    stamp duty     0.003% of the BUY premium        Indian Stamp Act, from 2020-07-01
    GST            18% on brokerage + exchange + SEBI fee
    brokerage      Rs 20 per executed order (Dhan F&O), charged on an exercise or assignment too
    not modelled   physical delivery of stock options in the money at expiry (the paper book
                   settles every option in cash at intrinsic value); NSE IPFT / clearing charges

F&O SEGMENT (W40, QR-05 / QR-06): FuturesCostModel charges a stock-futures leg of the W2
backtest (backtest/futures.py) -- the equity CostModel above is untouched. Preset
nse_futures, NSE equity futures as published for 2024-10-01 onward (unchanged at 2026-10;
check a recent contract note):

    STT            0.02% of the SELL-side value         Finance (No. 2) Act 2024, from 2024-10-01
                                                        (was 0.0125%); nothing on the buy side
    exchange       0.00173% of value (Rs 173 / crore)   NSE equity-futures transaction charge
                                                        from 2024-10-01 (SEBI's uniform-charges
                                                        circular of 2024-07)
    SEBI fee       0.0001% of value (Rs 10 / crore)
    stamp duty     0.002% of the BUY-side value         Indian Stamp Act, from 2020-07-01
    GST            18% on brokerage + exchange + SEBI fee
    brokerage      0.03% of the order value capped at Rs 20 per executed order (Dhan F&O:
                   "Rs 20 or 0.03%, whichever is lower"), plus brokerage_per_lot x lots
                   (0 here; the paper futures book charges futures.brokerage_per_lot = 20 per
                   lot -- set {"brokerage_pct": 0, "brokerage_max": null,
                   "brokerage_per_lot": 20} to match it)
    not modelled   NSE IPFT / clearing charges (fractions of a rupee per crore) and the
                   physical-delivery charges of a stock future held into expiry

    zero           no futures costs
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

DP_CHARGE_PER_SELL = 15.93      # Rs per sell, GST included -- see DP CHARGE above
ROUND_TRIP_PCT = 0.25           # the quick harnesses' blended round trip -- see ROUND TRIP above


def round_trip_cost(entry_value: float, exit_value: float, cost_pct: float = ROUND_TRIP_PCT) -> float:
    """A blended round trip in rupees: half of cost_pct on the entry leg's value, half on the exit
    leg's. Every harness that quotes "cost_pct round trip" charges it this one way."""
    return (abs(float(entry_value)) + abs(float(exit_value))) * float(cost_pct) / 200.0


@dataclass(frozen=True)
class CostModel:
    name: str = "nse_delivery"
    brokerage_pct: float = 0.0          # % of order value
    brokerage_min: float = 0.0          # Rs per order
    brokerage_max: float | None = None  # Rs cap per order
    stt_buy_pct: float = 0.1
    stt_sell_pct: float = 0.1
    exchange_txn_pct: float = 0.00297
    sebi_fee_pct: float = 0.0001
    stamp_buy_pct: float = 0.015
    gst_pct: float = 18.0               # on brokerage + exchange + SEBI fee
    dp_charge_per_sell: float = 0.0     # Rs per scrip per sell (depository), GST included; nse_delivery sets it
    flat_pct: float = 0.0               # 'flat' model: % of value per leg, nothing else

    def charges(self, side: str, value: float) -> dict:
        """Itemised charges for one leg. side 'BUY' or 'SELL'; value = qty x price."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        value = abs(float(value))
        if self.name == "flat":
            total = value * self.flat_pct / 100
            return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0,
                    "gst": 0.0, "dp": 0.0, "flat": round(total, 4), "total": round(total, 4)}
        brokerage = value * self.brokerage_pct / 100
        if value > 0:
            brokerage = max(brokerage, self.brokerage_min)
        if self.brokerage_max is not None:
            brokerage = min(brokerage, self.brokerage_max)
        stt = value * (self.stt_buy_pct if side == "BUY" else self.stt_sell_pct) / 100
        exchange = value * self.exchange_txn_pct / 100
        sebi = value * self.sebi_fee_pct / 100
        stamp = value * self.stamp_buy_pct / 100 if side == "BUY" else 0.0
        gst = (brokerage + exchange + sebi) * self.gst_pct / 100
        dp = self.dp_charge_per_sell if side == "SELL" and value > 0 else 0.0
        parts = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
                 "stamp": stamp, "gst": gst, "dp": dp}
        out = {k: round(v, 4) for k, v in parts.items()}
        out["total"] = round(sum(parts.values()), 4)
        return out

    def total(self, side: str, value: float) -> float:
        return self.charges(side, value)["total"]

    def as_dict(self) -> dict:
        return asdict(self)


PRESETS = {
    "nse_delivery": CostModel(name="nse_delivery", dp_charge_per_sell=DP_CHARGE_PER_SELL),
    "nse_intraday": CostModel(name="nse_intraday", brokerage_pct=0.03, brokerage_max=20.0,
                              stt_buy_pct=0.0, stt_sell_pct=0.025, stamp_buy_pct=0.003),
    "flat": CostModel(name="flat", flat_pct=0.125),
    "zero": CostModel(name="zero", stt_buy_pct=0.0, stt_sell_pct=0.0, exchange_txn_pct=0.0,
                      sebi_fee_pct=0.0, stamp_buy_pct=0.0, gst_pct=0.0),
}


def cost_model(name: str = "nse_delivery", overrides: dict | None = None) -> CostModel:
    """A preset, with any fields overridden. Unknown names/fields raise."""
    if name not in PRESETS:
        raise ValueError(f"unknown cost model {name!r}; one of {sorted(PRESETS)}")
    m = PRESETS[name]
    if overrides:
        bad = set(overrides) - set(m.as_dict())
        if bad:
            raise ValueError(f"unknown cost fields {sorted(bad)}")
        m = replace(m, **overrides)
    return m


def cost_model_from_config(cfg: dict | None = None) -> CostModel:
    """config.json's "backtest" section, or the nse_delivery default."""
    if cfg is None:
        from backtest.config import backtest_config
        cfg = backtest_config()
    return cost_model(cfg.get("cost_model", "nse_delivery"), cfg.get("cost_overrides") or None)


@dataclass(frozen=True)
class FuturesCostModel:
    """Charges for one stock-futures leg (the F&O segment; rates in the module docstring).
    value = lots x lot size x price (the notional); lots for the per-lot brokerage."""
    name: str = "nse_futures"
    brokerage_pct: float = 0.03          # % of order value ...
    brokerage_max: float | None = 20.0   # ... capped at Rs 20 per executed order (Dhan F&O)
    brokerage_per_lot: float = 0.0       # Rs per lot, on top
    stt_buy_pct: float = 0.0
    stt_sell_pct: float = 0.02
    exchange_txn_pct: float = 0.00173
    sebi_fee_pct: float = 0.0001
    stamp_buy_pct: float = 0.002
    gst_pct: float = 18.0                # on brokerage + exchange + SEBI fee

    def charges(self, side: str, value: float, lots: int = 1) -> dict:
        """Itemised charges for one futures leg. side 'BUY' or 'SELL'."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        value = abs(float(value))
        brokerage = value * self.brokerage_pct / 100
        if self.brokerage_max is not None:
            brokerage = min(brokerage, self.brokerage_max)
        if value > 0:
            brokerage += self.brokerage_per_lot * abs(int(lots))
        stt = value * (self.stt_buy_pct if side == "BUY" else self.stt_sell_pct) / 100
        exchange = value * self.exchange_txn_pct / 100
        sebi = value * self.sebi_fee_pct / 100
        stamp = value * self.stamp_buy_pct / 100 if side == "BUY" else 0.0
        gst = (brokerage + exchange + sebi) * self.gst_pct / 100
        parts = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp, "gst": gst}
        out = {k: round(v, 4) for k, v in parts.items()}
        out["total"] = round(sum(parts.values()), 4)
        return out

    def total(self, side: str, value: float, lots: int = 1) -> float:
        return self.charges(side, value, lots)["total"]

    def as_dict(self) -> dict:
        return asdict(self)


FUTURES_PRESETS = {
    "nse_futures": FuturesCostModel(),
    "zero": FuturesCostModel(name="zero", brokerage_pct=0.0, brokerage_max=None, stt_sell_pct=0.0,
                             exchange_txn_pct=0.0, sebi_fee_pct=0.0, stamp_buy_pct=0.0, gst_pct=0.0),
}


def futures_cost_model(name: str = "nse_futures", overrides: dict | None = None) -> FuturesCostModel:
    """An F&O preset, with any fields overridden. Unknown names / fields raise."""
    if name not in FUTURES_PRESETS:
        raise ValueError(f"unknown futures cost model {name!r}; one of {sorted(FUTURES_PRESETS)}")
    m = FUTURES_PRESETS[name]
    if overrides:
        bad = set(overrides) - set(m.as_dict())
        if bad:
            raise ValueError(f"unknown futures cost fields {sorted(bad)}")
        m = replace(m, **overrides)
    return m


@dataclass(frozen=True)
class OptionsCostModel:
    """Charges for one OPTION leg (NSE F&O; rates and sources in the module docstring). value = quantity
    x premium for a trade; for exercise() / assignment() the intrinsic value settled at expiry."""
    name: str = "nse_options"
    brokerage_per_order: float = 20.0    # Rs per executed order (Dhan F&O)
    stt_sell_pct: float = 0.15           # % of the SELL premium
    stt_exercise_pct: float = 0.15       # % of the intrinsic value of a long option exercised at expiry
    exchange_txn_pct: float = 0.03503    # % of premium
    sebi_fee_pct: float = 0.0001
    stamp_buy_pct: float = 0.003         # % of the BUY premium
    gst_pct: float = 18.0                # on brokerage + exchange + SEBI fee

    def _out(self, brokerage=0.0, stt=0.0, exchange=0.0, sebi=0.0, stamp=0.0) -> dict:
        gst = (brokerage + exchange + sebi) * self.gst_pct / 100
        parts = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp, "gst": gst}
        out = {k: round(v, 4) for k, v in parts.items()}
        out["total"] = round(sum(parts.values()), 4)
        return out

    def charges(self, side: str, value: float) -> dict:
        """Itemised charges for one premium trade. side 'BUY' or 'SELL'."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        value = abs(float(value))
        if value <= 0:
            return self._out()
        return self._out(brokerage=self.brokerage_per_order,
                         stt=value * self.stt_sell_pct / 100 if side == "SELL" else 0.0,
                         exchange=value * self.exchange_txn_pct / 100, sebi=value * self.sebi_fee_pct / 100,
                         stamp=value * self.stamp_buy_pct / 100 if side == "BUY" else 0.0)

    def exercise(self, value: float) -> dict:
        """A long option in the money at expiry: STT on the intrinsic value, the broker's order charge."""
        value = abs(float(value))
        if value <= 0:
            return self._out()
        return self._out(brokerage=self.brokerage_per_order, stt=value * self.stt_exercise_pct / 100)

    def assignment(self, value: float) -> dict:
        """A short option in the money at expiry: no STT for the writer, the broker's order charge."""
        return self._out(brokerage=self.brokerage_per_order) if abs(float(value)) > 0 else self._out()

    def total(self, side: str, value: float) -> float:
        return self.charges(side, value)["total"]

    def as_dict(self) -> dict:
        return asdict(self)


OPTIONS_PRESETS = {
    "nse_options": OptionsCostModel(),
    "zero": OptionsCostModel(name="zero", brokerage_per_order=0.0, stt_sell_pct=0.0, stt_exercise_pct=0.0,
                             exchange_txn_pct=0.0, sebi_fee_pct=0.0, stamp_buy_pct=0.0, gst_pct=0.0),
}


def options_cost_model(name: str = "nse_options", overrides: dict | None = None) -> OptionsCostModel:
    """An options preset, with any fields overridden. Unknown names / fields raise."""
    if name not in OPTIONS_PRESETS:
        raise ValueError(f"unknown options cost model {name!r}; one of {sorted(OPTIONS_PRESETS)}")
    m = OPTIONS_PRESETS[name]
    if overrides:
        bad = set(overrides) - set(m.as_dict())
        if bad:
            raise ValueError(f"unknown options cost fields {sorted(bad)}")
        m = replace(m, **overrides)
    return m
