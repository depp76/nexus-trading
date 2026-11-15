"""strategy/trend_following/variant_info.py — Plain-language description of each
6-2 matrix row (A0-A5, B, C1-C3) for the Strategy tab's variant picker.

Built from the `Variant` flags and the `StrategyParams` values rather than
written out per row, so a change to `config.py` changes the text with it.
The rules quoted are trend_following.md 2-1 (L1), 2-2 (L2), 2-3 (L3 + V/F),
2-4 (L4), 2-5 (C-series scoring), 3 (exits) and 6-2 (the matrix).

The user-facing strings are Korean by default (user direction, 2026-11-13: the
app's end user reads the descriptions in Korean), with an English partner per
string through `i18n.tr`; identifiers, comments and docstrings stay English per
the repo convention.
"""
from __future__ import annotations

from dataclasses import dataclass

from i18n import tr
from strategy.trend_following.config import VARIANTS, StrategyParams, Variant


@dataclass(frozen=True)
class VariantDescription:
    id: str
    title: str                                  # short list label, e.g. "돌파 + 국면 + 거래량"
    summary: str                                # what question this row answers in the matrix
    sections: tuple[tuple[str, tuple[str, ...]], ...]   # (heading, bullet lines)
    needs_flows: bool


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def _regime(p: StrategyParams) -> str:
    return tr(f"L1 시장 국면(매주): KOSPI 종가가 {p.regime_ma}일선 위이고 그 이동평균이 "
              f"{p.regime_slope_lookback}거래일 전보다 높을 때만 신규 진입. Risk-off 주에는 보유 종목을 "
              f"청산 규칙으로만 관리합니다.",
              f"L1 market regime (weekly): new entries only while KOSPI closes above its {p.regime_ma}-day MA "
              f"and that MA is higher than {p.regime_slope_lookback} sessions ago; in risk-off weeks held names "
              f"are managed by the exit rules only.")


def _trend(p: StrategyParams) -> str:
    return tr(f"L2 종목 추세(매일): 종가 > {p.trend_ma_long}일선, {p.trend_ma_long}일선이 "
              f"{p.trend_slope_lookback}거래일 전보다 상승, {p.trend_ma_short}일선 > {p.trend_ma_long}일선.",
              f"L2 stock trend (daily): close > MA{p.trend_ma_long}, MA{p.trend_ma_long} rising over "
              f"{p.trend_slope_lookback} sessions, and MA{p.trend_ma_short} > MA{p.trend_ma_long}.")


def _liquidity(p: StrategyParams) -> str:
    return tr(f"유동성 하한: 20일 평균 거래대금 {p.min_avg_trading_value / 1e8:,.0f}억 원 이상.",
              f"Liquidity floor: 20-day average trading value of at least "
              f"{p.min_avg_trading_value / 1e9:,g} billion KRW.")


def _sizing(p: StrategyParams) -> str:
    return tr(f"비중: 평가액의 {_pct(p.risk_per_trade)} 위험 / ({p.atr_multiple:g} × ATR{p.atr_window}), "
              f"종목당 상한 {_pct(p.max_weight)}(매주 초과분 매도), 정수 주, 최대 {p.max_positions}종목.",
              f"Sizing: {_pct(p.risk_per_trade)} of equity at risk / ({p.atr_multiple:g} × ATR{p.atr_window}), "
              f"capped at {_pct(p.max_weight)} per name (excess sold weekly), whole shares, "
              f"up to {p.max_positions} names.")


def _stop(p: StrategyParams) -> str:
    return tr(f"초기 손절: 종가 < 진입가 − {p.stop_atr_multiple:g} × ATR{p.atr_window}.",
              f"Initial stop: close < entry − {p.stop_atr_multiple:g} × ATR{p.atr_window}.")


def _donchian_exit(p: StrategyParams) -> str:
    return tr(f"추세 이탈: 종가 < 직전 {p.exit_window}거래일 최저가(Donchian {p.exit_window}).",
              f"Trend break: close < the lowest low of the prior {p.exit_window} sessions "
              f"(Donchian {p.exit_window}).")


def _exit_heading() -> str:
    return tr("청산 (매일 점검, 다음 날 시가 체결)", "Exit (checked daily, next-open fill)")


def _title(v: Variant) -> str:
    if v.mode == "high52":
        return tr("52주 고가 근접도 보유", "52-week-high proximity holding")
    if v.mode == "score":
        default = tr("추세 + 눌림 점수", "Trend + pullback score")
        return {"C1": default,
                "C2": tr("점수, 변동성 정규화 없음", "Score, raw (no ATR% scaling)"),
                "C3": tr("점수, 52주 고가 근접도", "Score, 52w-high proximity")}.get(v.id, default)
    if not v.regime:
        return tr("돌파만", "Breakout only")
    extras = [name for flag, name in ((v.volume, tr("거래량", "volume")), (v.flow, tr("수급", "flows")),
                                      (v.flow_exit, tr("수급 청산", "flow exit"))) if flag]
    return tr("돌파 + 국면", "Breakout + regime") + (" + " + " + ".join(extras) if extras else "")


def _summary(v: Variant) -> str:
    return {
        "A0": tr("A 계열의 기준선. 시장 필터 없이 Donchian 돌파만 쓰며, 나머지 행은 모두 이것과 비교합니다.",
                 "Baseline of the A-series: the Donchian breakout with no market filter, so every other row "
                 "is measured against it."),
        "A1": tr("주간 KOSPI 국면 필터가 도움이 되는가? A0에 L1을 더한 것.",
                 "Does the weekly KOSPI regime filter help? Same as A0 plus L1."),
        "A2": tr("돌파일 거래량 급증 확인이 도움이 되는가? A1에 V 필터를 더한 것.",
                 "Does a volume surge on the breakout day help? A1 plus the V filter."),
        "A3": tr("외국인 + 기관 순매수 확인이 도움이 되는가? A1에 F 필터를 더한 것.",
                 "Does foreign + institution buying help? A1 plus the F filter."),
        "A4": tr("거래량과 수급 확인을 함께: A1에 V와 F를 더한 것.",
                 "Volume and flow confirmation together: A1 plus V and F."),
        "A5": tr("A 계열 전체: A4에 수급 기반 청산 FX를 더한 것.",
                 "The full A-series: A4 plus the flow-based exit FX."),
        "B": tr("돌파 매수 대신 52주 고가에 가장 가까운 종목을 보유합니다.",
                "Instead of buying breakouts, hold the names closest to their 52-week high."),
        "C1": tr("2-5 스코어링 모드: 추세가 강하면서 최근 며칠 쉬어간(눌린) 종목을 삽니다.",
                 "The 2-5 scoring mode: buy names in a strong trend that have pulled back over the last "
                 "few days."),
        "C2": tr("C1에서 단기 지표를 ATR%로 나누지 않음. 변동성 정규화가 의미 있는지 확인합니다.",
                 "C1 without dividing the short-term indicators by ATR%, to see if volatility "
                 "normalisation matters."),
        "C3": tr("C1에서 추세 위치를 Range52 대신 종가 / 52주 최고가로 잽니다.",
                 "C1 with the trend position measured as close / 52-week high instead of Range52."),
    }.get(v.id, v.label)


def describe_variant(vid: str, params: StrategyParams | None = None) -> VariantDescription:
    """The description of matrix row `vid` with the numbers of `params`
    (the StrategyParams defaults when None)."""
    v = VARIANTS[vid]
    p = params or StrategyParams()
    if v.mode == "score":
        p = v.apply(p)
    sections: list[tuple[str, tuple[str, ...]]] = []

    universe = [_liquidity(p)]
    if v.regime:
        universe.append(_regime(p))
    sections.append((tr("시장과 유니버스", "Market and universe"), tuple(universe)))

    if v.mode == "breakout":
        entry = [_trend(p),
                 tr(f"L3 진입(매일): 종가가 직전 {p.breakout_window}거래일 최고가를 넘음"
                    f"(Donchian {p.breakout_window}), 다음 날 시가 체결.",
                    f"L3 trigger (daily): close above the highest high of the prior {p.breakout_window} "
                    f"sessions (Donchian {p.breakout_window}); filled at the next session's open.")]
        if v.volume:
            entry.append(tr(f"V: 돌파일 거래량 ≥ {p.volume_window}일 평균 거래량 × {p.volume_ratio_min:g}.",
                            f"V: breakout-day volume ≥ {p.volume_ratio_min:g} × its {p.volume_window}-day average."))
        if v.flow:
            entry.append(tr(f"F: 최근 {p.flow_long_window}일과 {p.flow_short_window}일 (외국인 + 기관) "
                            f"누적 순매수가 모두 0보다 큼.",
                            f"F: foreign + institution net buying over the last {p.flow_long_window} and "
                            f"{p.flow_short_window} sessions both > 0."))
        entry.append(tr(f"L4 순위(후보가 빈 자리보다 많을 때): 20일 수급강도 순위 + {p.rs_window}일 KOSPI 대비 "
                        f"상대강도 순위 (수급 데이터가 없으면 상대강도만).",
                        f"L4 ranking when candidates exceed open slots: rank of 20-day flow strength + rank of "
                        f"{p.rs_window}-day return relative to KOSPI (relative strength only without flow data)."))
        entry.append(_sizing(p))
        sections.append((tr("진입", "Entry"), tuple(entry)))
        exits = [_donchian_exit(p), _stop(p)]
        if v.flow_exit:
            exits.append(tr(f"FX: 최근 {p.flow_exit_window}일 (외국인 + 기관) 누적 순매도 "
                            f"그리고 종가 < {p.trend_ma_short}일선.",
                            f"FX: foreign + institution net selling over {p.flow_exit_window} sessions "
                            f"and close < MA{p.trend_ma_short}."))
        sections.append((_exit_heading(), tuple(exits)))
    elif v.mode == "high52":
        sections.append((tr("보유 규칙 (주간 점검일)", "Holding rule (weekly check day)"), (
            _trend(p),
            tr(f"L2와 유동성 하한을 통과한 종목을 종가 / {p.high_52w_window}일 최고가 순으로 정렬해 "
               f"상위 {p.max_positions}종목을 동일 비중(종목당 ≤ {_pct(p.max_weight)})으로 보유.",
               f"Rank the names passing L2 and the liquidity floor by close / {p.high_52w_window}-day high "
               f"and hold the top {p.max_positions} at equal weight (≤ {_pct(p.max_weight)} each)."),
            tr("순위 밖으로 밀려난 종목은 다음 날 시가에 매도.",
               "Names that drop out of the top ranks are sold at the next open."),
        )))
        sections.append((_exit_heading(), (_donchian_exit(p), _stop(p))))
    else:
        range_ind = (tr(f"종가 / {p.score_range_window}일 최고가", f"close / {p.score_range_window}-day high")
                     if p.score_range_mode == "high52_prox"
                     else tr(f"Range52 = {p.score_range_window}일 최저~최고 범위 안에서 종가의 위치",
                             f"Range52 = position of the close in its {p.score_range_window}-day low-high range"))
        norm = (tr("R3와 MA20Div를 ATR20 / 종가로 나눔(변동성 정규화).",
                   "R3 and MA20Div are divided by ATR20 / close (volatility normalised).")
                if p.score_vol_normalize
                else tr("R3와 MA20Div를 원래 수익률 그대로 사용(변동성 정규화 없음).",
                        "R3 and MA20Div are used as raw returns (no volatility normalisation)."))
        sections.append((tr("게이트 (모두 통과해야 함)", "Gate (all must pass)"), (
            tr(f"종가가 {p.score_ma_long}일선 위(MA50Div > 0).",
               f"Close above MA{p.score_ma_long} (MA50Div > 0)."),
            tr(f"추세 위치 ≥ {p.gate_range_min:g}, 기준: {range_ind}.",
               f"Trend position ≥ {p.gate_range_min:g}, using {range_ind}."),
            tr(f"과열 아님: MA20Div가 유동성 충족 종목 중 상위 {(1 - p.gate_overheat_pct) * 100:g}% 밖.",
               f"Not overheated: MA20Div outside the top {(1 - p.gate_overheat_pct) * 100:g}% of liquid names."),
        )))
        sections.append((tr("점수", "Score"), (
            tr(f"추세점수 = pct(MA50Div) + pct(추세 위치), 타이밍점수 = −pct(R{p.score_r_short}): "
               f"최근에 눌린 종목일수록 점수가 높음. 가중치 1 : 1 : 1.",
               f"Trend score = pct(MA50Div) + pct(trend position); timing score = −pct(R{p.score_r_short}), "
               f"so a recent dip scores higher. Weights 1 : 1 : 1."),
            norm,
        )))
        sections.append((tr("매수 (주간 점검일, 다음 날 시가 체결)", "Buy (weekly check day, next-open fill)"), (
            tr(f"Risk-on 주, 게이트 통과, 총점 순위 ≥ {p.score_buy_pct:g}. 점수 높은 순으로 빈 자리를 채움.",
               f"Risk-on week, gate passed and total-score percentile ≥ {p.score_buy_pct:g}; "
               f"best scores first into the open slots."),
            _sizing(p),
        )))
        sections.append((tr("매도", "Sell"), (
            tr(f"매주: 총점 순위 < {p.score_sell_pct:g} (매수 문턱보다 낮게 두어 잦은 매매를 막음).",
               f"Weekly: score percentile < {p.score_sell_pct:g} (a lower bar than the buy, to avoid churning)."),
            tr(f"매일: 종가가 {p.score_ma_long}일선 아래, 또는 초기 손절"
               f"(종가 < 진입가 − {p.stop_atr_multiple:g} × ATR{p.atr_window}).",
               f"Daily: close below MA{p.score_ma_long}, or the initial stop "
               f"(close < entry − {p.stop_atr_multiple:g} × ATR{p.atr_window})."),
            tr("Donchian 10 청산은 쓰지 않으며, 과열 종목도 계속 보유(과열은 신규 진입만 막음).",
               "No Donchian 10 exit, and an overheated holding is kept (overheat only blocks new entries)."),
        )))

    if v.needs_flows:
        data = [tr("투자자 수급 데이터 필요(고르면 실행할 때 함께 받음, 한국 종목만). 받지 못하면 실행에서 제외됩니다.",
                   "Needs investor-flow data (fetched with the run when picked, KR stocks only); "
                   "skipped if it cannot be loaded.")]
    else:
        data = [tr("가격 데이터만 사용. 수급 데이터 없이 실행됩니다.",
                   "Price data only; runs without investor flows.")]
    sections.append((tr("데이터", "Data"), tuple(data)))
    return VariantDescription(v.id, _title(v), _summary(v), tuple(sections), v.needs_flows)
