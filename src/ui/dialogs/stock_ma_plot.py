"""ui/dialogs/stock_ma_plot.py — _PlotMixin: the plotting half of the MA chart
dialog (ui/dialogs/stock_ma.py StockMaDialog).

Builds the panels (price + MA ramp, volume, divergence, RSI) in fixed slots
(_apply_layout), switches series / panels on and off, sizes the figure
margins from the labels (_fit_margins and its helpers) and places the
right-edge value labels (_place_end_labels / _end_label_style).

Dialog state it uses
--------------------
reads   _df, _is_simple_chart, _fmt_str, _unit_suffix, _fig, _canvas,
        _series_buttons, _panel_buttons, _cursor_idx, _x_lo, _x_hi;
        _col() (dialog), _autofit_y / _install_pan_zoom / _apply_period
        (_ViewMixin), _install_crosshair / _update_hover (_HoverMixin)
writes  _x, _dates, _panel_axes, _panel_on, _series_on, _lines,
        _end_labels, _crosshairs, _div_bands, _margins

Moved out of ui/dialogs/stock_ma.py (2026-11-15); method names unchanged.
"""
import numpy as np
import polars as pl
import matplotlib.dates as mdates
from matplotlib.collections import PolyCollection

from i18n import tr
from ui.dialogs.stock_ma_consts import (
    _DEFAULT_MARGINS, _DEFAULT_PERIOD, _DEFAULT_SERIES, _MARGIN_PAD_PX, _MIN_RIGHT_PX, _PRICE_SERIES,
    _SLOT_RATIOS, _TICK_PT,
)
from ui.dialogs.stock_ma_helpers import _make_bars, _compact
from ui.colors import PROFIT, LOSS, MA_RAMP, MA_DIV_NEUTRAL_PCT
from ui.theme import ACCENT, TEXT, TEXT_MUTED, TEXT_FAINT, LINE, INK_ON_LIGHT



class _PlotMixin:
    """Panel plotting, slot layout, margins and end labels for StockMaDialog."""

    def _plot(self) -> None:
        df = self._df
        dates = df.get_column("Date").to_numpy()
        self._x = mdates.date2num(dates)
        self._dates = dates

        has_vol = "Volume" in df.columns and float(df.get_column("Volume").max() or 0) > 0
        has_div = not self._is_simple_chart and ("MA20_Div" in df.columns or "MA50_Div" in df.columns)
        has_rsi = "RSI14" in df.columns

        gs = self._fig.add_gridspec(4, 1)
        ax_price = self._fig.add_subplot(gs[0])
        self._panel_axes["price"] = ax_price
        if has_vol:
            self._panel_axes["vol"] = self._fig.add_subplot(gs[1], sharex=ax_price)
        if has_div:
            self._panel_axes["div"] = self._fig.add_subplot(gs[2], sharex=ax_price)
        if has_rsi and not self._is_simple_chart:
            self._panel_axes["rsi"] = self._fig.add_subplot(gs[3], sharex=ax_price)

        self._plot_price_panel()
        if "vol" in self._panel_axes:
            self._plot_volume_panel()
        if "div" in self._panel_axes:
            self._plot_divergence_panel()
        if "rsi" in self._panel_axes:
            self._plot_rsi_panel()

        for key in ("vol", "div", "rsi"):
            available = key in self._panel_axes
            default_on = available and (key == "vol" or (key == "div" and "Div" in _DEFAULT_SERIES))
            self._panel_on[key] = default_on
            btn = self._panel_buttons.get(key) or self._series_buttons.get("Div" if key == "div" else "")
            if btn is not None:
                btn.setVisible(available)
                btn.setChecked(default_on)

        for ax in self._panel_axes.values():
            ax.tick_params(axis="both", labelsize=_TICK_PT, colors=TEXT_MUTED, length=3)
            ax.grid(True, linestyle=":", color=LINE, alpha=0.9)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                ax.spines[spine].set_color(LINE)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=9))
            self._crosshairs.append(ax.axvline(self._x[-1], color=ACCENT, linewidth=1, linestyle=(0, (3, 3)),
                                               visible=False, zorder=6))

        self._install_pan_zoom()
        self._install_crosshair()
        if self._is_simple_chart:
            self._series_on = {k: (k == "Close") for k in _PRICE_SERIES}
        self._apply_series_visibility()
        self._apply_layout()
        self._apply_period(_DEFAULT_PERIOD)
        self._fit_margins()                 # baseline for the widest labels; toggles keep it

    def _plot_price_panel(self) -> None:
        """Close (ink) + MA ramp; Equal Weight rebased onto the same axis."""
        ax = self._panel_axes["price"]
        x = self._x
        self._lines["Close"], = ax.plot(x, self._col("Close"), color=TEXT, linewidth=1.7, label="Close", zorder=5)
        if not self._is_simple_chart:
            for key, color in MA_RAMP.items():
                arr = self._col(key)
                if arr is not None:
                    self._lines[key], = ax.plot(x, arr, color=color, linewidth=1.3, label=key, zorder=4)
        ew = self._col("EqualWeight")
        if ew is not None and not self._is_simple_chart:
            close = self._col("Close")
            both = ~(np.isnan(ew) | np.isnan(close))
            if both.any():
                first = int(np.argmax(both))
                rebased = ew * (close[first] / ew[first])
                self._lines["EW"], = ax.plot(x, rebased, color=TEXT_MUTED, linewidth=1.2, linestyle="--",
                                             label="EW", zorder=3, visible=False)
        ax.set_ylabel(tr("가격", "Price") + (f" ({self._unit_suffix})" if self._unit_suffix else ""),
                      fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.yaxis.set_major_formatter(lambda v, _pos: self._fmt_str.format(v))
        ax.tick_params(labelbottom=False)

    def _plot_volume_panel(self) -> None:
        """Up/down volume bars in PROFIT/LOSS (issue #1)."""
        ax = self._panel_axes["vol"]
        df = self._df
        up = df.filter(pl.col("Close") >= pl.col("Open"))
        down = df.filter(pl.col("Close") < pl.col("Open"))
        width = 0.62
        for part, color, alpha in ((up, PROFIT, 0.55), (down, LOSS, 0.5)):
            if part.height:
                xs = mdates.date2num(part.get_column("Date").to_numpy())
                ax.add_collection(PolyCollection(
                    _make_bars(xs, [0] * part.height, part.get_column("Volume").to_numpy(), width),
                    facecolors=color, alpha=alpha, linewidths=0))
        ax.set_ylabel(tr("거래량", "Volume"), fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.yaxis.set_major_formatter(lambda v, _pos: _compact(v) if v else "0")
        ax.tick_params(labelbottom=False)

    def _plot_divergence_panel(self) -> None:
        """Div(20)/Div(50) = price / MA x 100 over three bands centred on 100
        (issue #6): above 100+MA_DIV_NEUTRAL_PCT overheated (PROFIT tint),
        below 100-MA_DIV_NEUTRAL_PCT depressed (LOSS tint), neutral between."""
        ax = self._panel_axes["div"]
        n = MA_DIV_NEUTRAL_PCT
        self._div_bands = [
            ax.axhspan(100 + n, 10_000, facecolor=PROFIT, alpha=0.09, linewidth=0),
            ax.axhspan(100 - n, 100 + n, facecolor=TEXT_FAINT, alpha=0.07, linewidth=0),
            ax.axhspan(-10_000, 100 - n, facecolor=LOSS, alpha=0.08, linewidth=0),
        ]
        ax.axhline(100, color=TEXT_FAINT, linewidth=1, linestyle=(0, (4, 3)))
        if "MA50_Div" in self._df.columns:
            self._lines["Div(50)"], = ax.plot(self._x, self._col("MA50_Div"), color=MA_RAMP["MA20"], linewidth=1.4)
        if "MA20_Div" in self._df.columns:
            self._lines["Div(20)"], = ax.plot(self._x, self._col("MA20_Div"), color=MA_RAMP["MA5"], linewidth=1.4)
        ax.set_ylabel(tr("이격도 %", "Div %"), fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.tick_params(labelbottom=False)

    def _plot_rsi_panel(self) -> None:
        """RSI14 in its own panel (issue #4); overbought = PROFIT, oversold = LOSS."""
        ax = self._panel_axes["rsi"]
        rsi = self._col("RSI14")
        self._lines["RSI"], = ax.plot(self._x, rsi, color=ACCENT, linewidth=1.3)
        ax.axhline(70, color=PROFIT, linestyle=":", alpha=0.6, linewidth=1)
        ax.axhline(30, color=LOSS, linestyle=":", alpha=0.6, linewidth=1)
        ax.fill_between(self._x, 70, 100, where=(rsi >= 70), facecolor=PROFIT, alpha=0.1, linewidth=0)
        ax.fill_between(self._x, 0, 30, where=(rsi <= 30), facecolor=LOSS, alpha=0.1, linewidth=0)
        ax.set_ylim(0, 100)
        ax.set_yticks([30, 50, 70])
        ax.set_ylabel("RSI 14", fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.tick_params(labelbottom=False)

    # --------------------------------------------------------- layout / view
    def _visible_panels(self) -> list:
        """[(panel, slot ratio)] of the panels shown, top to bottom. The price
        panel is hidden when no price line is selected and another panel is
        on (an empty frame when nothing is on at all)."""
        others = [k for k in ("vol", "div", "rsi") if self._panel_on[k] and k in self._panel_axes]
        order = ["price"] if (self._price_series_visible() or not others) else []
        return [(k, _SLOT_RATIOS[k]) for k in order + others]

    def _slots(self) -> list:
        return ["price"] + [k for k in ("vol", "div", "rsi") if k in self._panel_axes]

    def _apply_layout(self) -> None:
        """Every panel the instrument has sits in its own fixed slot (price
        3 : volume 1 : divergence 1 : RSI 1); switching one off hides it and
        leaves the slot empty, so nothing else moves or resizes (2026-11-15;
        it used to re-grid the visible panels, issue #11). The date labels go
        under the lowest panel shown."""
        slots = self._slots()
        left, right = getattr(self, "_margins", _DEFAULT_MARGINS)
        gs = self._fig.add_gridspec(len(slots), 1, height_ratios=[_SLOT_RATIOS[k] for k in slots],
                                    hspace=0.08, left=left, right=right, top=0.97, bottom=0.07)
        shown = [k for k, _r in self._visible_panels()]
        bottom = max(shown, key=slots.index) if shown else None
        for i, key in enumerate(slots):
            ax = self._panel_axes[key]
            ax.set_subplotspec(gs[i])
            ax.set_visible(key in shown)
            ax.tick_params(labelbottom=(key == bottom))

    def _fit_margins(self, grow_only: bool = False) -> bool:
        """Left/right margins in pixels from what has to fit there, so neither
        the Y labels nor the end-value labels are cut in a narrow dialog
        (fixed 7.5% / 10% fractions cut "100,000"). The baseline (on plot and
        on resize) is measured for the widest case -- every panel's Y labels
        over the full data range, every price series' end label -- so a
        toggle does not move the chart (2026-11-15). After that a pan, zoom
        or toggle only widens a margin when something would be cut
        (`grow_only`). Re-grids and returns True when a margin moved."""
        if self._x is None:
            return False
        fig_w = self._fig.bbox.width
        if fig_w <= 0:
            return False
        renderer = self._canvas.get_renderer()
        if grow_only:
            left_px = self._left_px(renderer, [ax for ax in self._panel_axes.values() if ax.get_visible()])
            right_px = self._right_px_current(renderer)
        else:
            left_px = self._left_px_full_range(renderer)
            right_px = self._right_px_widest(renderer)
        left = min(0.4, (left_px + _MARGIN_PAD_PX) / fig_w)
        right = max(0.6, 1.0 - (right_px + _MARGIN_PAD_PX) / fig_w)
        old_l, old_r = getattr(self, "_margins", _DEFAULT_MARGINS)
        if grow_only:
            left, right = max(left, old_l), min(right, old_r)
        if abs(left - old_l) * fig_w < 1 and abs(right - old_r) * fig_w < 1:
            return False
        self._margins = (float(left), float(right))
        self._apply_layout()
        return True

    @staticmethod
    def _left_px(renderer, axes) -> float:
        out = 0.0
        for ax in axes:
            bb = ax.yaxis.get_tightbbox(renderer)
            if bb is not None:
                out = max(out, ax.bbox.x0 - bb.x0)
        return out

    def _left_px_full_range(self, renderer) -> float:
        """Y-label width of every panel (shown or not) with its Y range set
        to the whole data: the widest tick labels it normally gets."""
        price = self._panel_axes["price"]
        saved_x = price.get_xlim()
        saved_y = {k: ax.get_ylim() for k, ax in self._panel_axes.items()}
        try:
            price.set_xlim(self._x_lo, self._x_hi)
            self._autofit_y()
            vals = [np.asarray(line.get_ydata(), dtype=float) for _k, line in self._price_lines()]
            vals = np.concatenate(vals) if vals else np.array([])
            vals = vals[np.isfinite(vals)]
            if len(vals):
                price.set_ylim(float(vals.min()), float(vals.max()))
            return self._left_px(renderer, list(self._panel_axes.values()))
        finally:
            price.set_xlim(*saved_x)
            for k, ax in self._panel_axes.items():
                ax.set_ylim(*saved_y[k])

    def _price_lines(self) -> list:
        """[(key, Line2D)] of every price-panel series, shown or not."""
        return [(k, self._lines[k]) for k in ("Close", *MA_RAMP.keys(), "EW") if k in self._lines]

    def _right_px_current(self, renderer) -> float:
        price = self._panel_axes["price"]
        out = float(_MIN_RIGHT_PX)
        for ann in self._end_labels.values():
            out = max(out, ann.get_window_extent(renderer).x1 - price.bbox.x1)
        return out

    def _right_px_widest(self, renderer) -> float:
        """The widest end label any price series can get: each series' label
        at its largest value, measured as a throwaway annotation."""
        price = self._panel_axes["price"]
        out = float(_MIN_RIGHT_PX)
        x_right = price.get_xlim()[1]
        for key, line in self._price_lines():
            ys = np.asarray(line.get_ydata(), dtype=float)
            ys = ys[np.isfinite(ys)]
            if not len(ys):
                continue
            text = self._end_label_style(key, float(np.max(np.abs(ys))))[0]
            ann = price.annotate(text, xy=(x_right, 0), xycoords=("data", "axes fraction"), xytext=(5, 0),
                                 textcoords="offset points", fontsize=_TICK_PT, ha="left", va="center",
                                 annotation_clip=False, bbox=dict(boxstyle="round,pad=0.25", edgecolor="none"))
            out = max(out, ann.get_window_extent(renderer).x1 - price.bbox.x1)
            ann.remove()
        return out

    def _set_panel(self, key: str, on: bool) -> None:
        self._panel_on[key] = bool(on)
        self._refresh_after_toggle()

    def _set_series(self, key: str, on: bool) -> None:
        """Toggle one series independently of the others (user direction
        2026-10-15): a price-panel line, or "Div" for the divergence panel."""
        btn = self._series_buttons.get(key)
        if btn is not None and btn.isChecked() != bool(on):
            btn.setChecked(bool(on))
        if key == "Div":
            self._panel_on["div"] = bool(on)
        else:
            self._series_on[key] = bool(on)
            self._apply_series_visibility()
        self._refresh_after_toggle()

    def _apply_series_visibility(self) -> None:
        for name, line in self._lines.items():
            if name in self._series_on:
                line.set_visible(self._series_on[name])

    def _refresh_after_toggle(self) -> None:
        if self._x is None:
            return
        self._apply_layout()
        self._autofit_y()
        self._place_end_labels()
        self._fit_margins(grow_only=True)
        self._update_hover(self._cursor_idx)
        self._canvas.draw_idle()

    # ------------------------------------------------------------ end labels
    def _price_series_visible(self) -> list:
        """[(key, Line2D)] of the price-panel lines currently shown, Close first."""
        out = []
        for key in ("Close", *MA_RAMP.keys(), "EW"):
            line = self._lines.get(key)
            if line is not None and line.get_visible():
                out.append((key, line))
        return out

    def _end_label_style(self, key: str, y: float) -> tuple:
        """(text, background, text color) of a price series' end label."""
        if key == "Close":
            return self._fmt_str.format(y), TEXT, INK_ON_LIGHT
        if key == "EW":
            return tr(f"동일가중 {_compact(y)}", f"EW {_compact(y)}"), TEXT_MUTED, INK_ON_LIGHT
        return f"{key} {_compact(y)}", MA_RAMP[key], INK_ON_LIGHT     # every MA_RAMP step is light

    def _place_end_labels(self) -> None:
        """A value label at each visible price-panel line's right edge (issue
        #3, replacing the legend), pushed apart vertically so they never
        overlap (same rule as the mockup: keep anchor order, min pitch)."""
        ax = self._panel_axes["price"]
        for ann in self._end_labels.values():
            ann.remove()
        self._end_labels.clear()

        x_right = ax.get_xlim()[1]
        idx = int(np.searchsorted(self._x, x_right, side="right") - 1)
        idx = max(0, min(len(self._x) - 1, idx))
        entries = []
        if not ax.get_visible():
            return
        for key, line in self._price_series_visible():
            y = line.get_ydata()[idx]
            if y is None or not np.isfinite(y):
                continue
            text, bg, fg = self._end_label_style(key, y)
            entries.append((key, float(y), text, bg, fg))
        if not entries:
            return

        # Collision push in display pixels, then back to an offset in points.
        dpi = self._fig.dpi
        pitch_px = 1.6 * _TICK_PT * dpi / 72
        anchored = sorted(((ax.transData.transform((x_right, y))[1], key, y, text, bg, fg)
                           for key, y, text, bg, fg in entries), key=lambda t: t[0])
        placed = []
        for disp_y, key, y, text, bg, fg in anchored:
            if placed and disp_y - placed[-1][0] < pitch_px:
                disp_y = placed[-1][0] + pitch_px
            placed.append([disp_y, key, y, text, bg, fg])
        top_px = ax.transAxes.transform((0, 1))[1]
        overflow = placed[-1][0] - top_px + pitch_px / 2
        if overflow > 0:
            for p in placed:
                p[0] -= overflow

        for disp_y, key, y, text, bg, fg in placed:
            anchor_px = ax.transData.transform((x_right, y))[1]
            dy_pt = (disp_y - anchor_px) * 72 / dpi
            self._end_labels[key] = ax.annotate(
                text, xy=(x_right, y), xytext=(5, dy_pt), textcoords="offset points",
                fontsize=_TICK_PT, color=fg, ha="left", va="center", annotation_clip=False, zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", facecolor=bg, edgecolor="none"),
            )
