"""ui/dialogs/stock_ma_view.py — _ViewMixin: the visible range of the MA chart
dialog (ui/dialogs/stock_ma.py StockMaDialog).

Mouse-wheel zoom, left-drag pan (the drag itself is followed in
_HoverMixin._on_motion), the horizontal scrollbar, the period buttons
(_apply_period), the per-panel Y autofit (_autofit_y) and the bond-only
Y zoom (_zoom_y).

Dialog state it uses
--------------------
reads   _x, _dates, _panel_axes, _period_combo, _canvas; _col() (dialog),
        _price_series_visible / _place_end_labels / _fit_margins (_PlotMixin)
writes  _x_lo, _x_hi, scrollbar, _pan_start

Moved out of ui/dialogs/stock_ma.py (2026-11-15); method names unchanged.
"""
import numpy as np
import pandas as pd
import matplotlib.dates as mdates
from PyQt6.QtWidgets import QScrollBar
from PyQt6.QtCore import Qt

from ui.dialogs.stock_ma_consts import _PERIODS, _SCROLL_MAX
from ui.colors import MA_DIV_NEUTRAL_PCT



class _ViewMixin:
    """Pan / zoom / scrollbar / period / Y autofit for StockMaDialog."""

    def _install_pan_zoom(self) -> None:
        """Mouse-wheel zoom, left-drag pan and a horizontal scrollbar, all
        clamped to the data range (no right padding so future dates never appear)."""
        abs_min, abs_max = float(self._x[0]), float(self._x[-1])
        self._x_lo = abs_min - (abs_max - abs_min) * 0.02
        self._x_hi = abs_max

        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_change)

        canvas = self._canvas
        canvas.mpl_connect('scroll_event', self._on_scroll)
        canvas.mpl_connect('button_press_event', self._on_press)
        canvas.mpl_connect('button_release_event', self._on_release)

    def _chart_axes(self) -> list:
        return [ax for ax in self._panel_axes.values() if ax.get_visible()]

    def _clamp_xlim(self, new_xmin: float, new_xmax: float) -> tuple:
        w = new_xmax - new_xmin
        if w > (self._x_hi - self._x_lo):
            return self._x_lo, self._x_hi
        if new_xmin < self._x_lo:
            new_xmin = self._x_lo
            new_xmax = new_xmin + w
        if new_xmax > self._x_hi:
            new_xmax = self._x_hi
            new_xmin = new_xmax - w
        return new_xmin, new_xmax

    def _set_xlim(self, xmin: float, xmax: float) -> None:
        self._panel_axes["price"].set_xlim(*self._clamp_xlim(xmin, xmax))
        self._autofit_y()
        self._place_end_labels()
        self._fit_margins(grow_only=True)   # only if a zoomed-in range needs wider labels
        self._update_scrollbar()
        self._canvas.draw_idle()

    def _visible_mask(self):
        xmin, xmax = self._panel_axes["price"].get_xlim()
        return (self._x >= xmin) & (self._x <= xmax)

    def _autofit_y(self) -> None:
        """Fit each panel's Y range to the bars in view (with the period
        buttons a 1M window on a 5Y Y range would be a flat line)."""
        mask = self._visible_mask()
        if not mask.any():
            return
        self._rescale_price_lines(mask)

        def span(arrays, pad):
            arrays = [np.asarray(a, dtype=float)[mask] for a in arrays if a is not None]
            if not arrays:
                return None
            vals = np.concatenate(arrays)
            vals = vals[np.isfinite(vals)]
            if len(vals) == 0:
                return None
            lo, hi = float(vals.min()), float(vals.max())
            margin = (hi - lo) * pad or abs(hi) * 0.01 or 1.0
            return lo - margin, hi + margin

        price_arrays = [line.get_ydata() for _k, line in self._price_series_visible()]
        rng = span(price_arrays, 0.04)
        if rng:
            self._panel_axes["price"].set_ylim(*rng)

        if "vol" in self._panel_axes:
            vol = self._col("Volume")
            if vol is not None:
                v = np.asarray(vol, dtype=float)[mask]
                v = v[np.isfinite(v)]
                if len(v) and v.max() > 0:
                    self._panel_axes["vol"].set_ylim(0, float(v.max()) * 1.1)

        if "div" in self._panel_axes:
            rng = span([self._col("MA20_Div"), self._col("MA60_Div")], 0.0)
            if rng:
                # Symmetric about 100 so the baseline sits mid-panel (issue #6).
                r = max(abs(rng[1] - 100), abs(100 - rng[0])) + MA_DIV_NEUTRAL_PCT
                self._panel_axes["div"].set_ylim(100 - r, 100 + r)

    def _update_scrollbar(self) -> None:
        x_min, x_max = self._panel_axes["price"].get_xlim()
        denom = (self._x_hi - self._x_lo) - (x_max - x_min)
        val = 0 if denom <= 0 else int((x_min - self._x_lo) / denom * _SCROLL_MAX)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, _SCROLL_MAX)
        self.scrollbar.setValue(max(0, min(_SCROLL_MAX, val)))
        self.scrollbar.blockSignals(False)

    def _on_scrollbar_change(self, val: int) -> None:
        x_min, x_max = self._panel_axes["price"].get_xlim()
        w = x_max - x_min
        new_xmin = self._x_lo + (val / _SCROLL_MAX) * ((self._x_hi - self._x_lo) - w)
        self._set_xlim(new_xmin, new_xmin + w)

    def _on_period_clicked(self, label: str, months) -> None:
        self._apply_period(label, months)

    def _apply_period(self, label: str, months=None) -> None:
        combo = self._period_combo
        i = combo.findData(label)
        if i >= 0 and combo.currentIndex() != i:
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
        if months is None:
            months = dict(_PERIODS).get(label)
        if months is None:
            self._set_xlim(self._x_lo, self._x_hi)
            return
        end = pd.to_datetime(self._dates[-1])
        start = mdates.date2num(end - pd.DateOffset(months=months))
        self._set_xlim(max(start, self._x_lo), self._x_hi)

    def _on_scroll(self, event) -> None:
        if event.inaxes not in self._chart_axes() or event.xdata is None:
            return
        scale = 1.15
        if event.button == 'up':
            factor = 1 / scale        # zoom in
        elif event.button == 'down':
            factor = scale            # zoom out
        else:
            return
        x_min, x_max = self._panel_axes["price"].get_xlim()
        x_focus = event.xdata
        self._set_xlim(x_focus - (x_focus - x_min) * factor, x_focus + (x_max - x_focus) * factor)

    def _on_press(self, event) -> None:
        if event.button != 1 or event.inaxes not in self._chart_axes():
            return
        self._pan_start = (event.x, self._panel_axes["price"].get_xlim())

    def _on_release(self, _event) -> None:
        self._pan_start = None

    def _zoom_y_in(self, *_):
        self._zoom_y(0.8)

    def _zoom_y_out(self, *_):
        self._zoom_y(1.2)

    def _zoom_y(self, factor: float) -> None:
        """Scale the price panel's Y range around the midpoint of the visible closes."""
        ax = self._panel_axes["price"]
        ymin, ymax = ax.get_ylim()
        try:
            close = np.asarray(self._col("Close"), dtype=float)[self._visible_mask()]
            close = close[np.isfinite(close)]
            ymid = (close.min() + close.max()) / 2 if len(close) else (ymin + ymax) / 2
        except Exception:
            ymid = (ymin + ymax) / 2
        ydiff = (ymax - ymin) / 2
        ax.set_ylim(ymid - ydiff * factor, ymid + ydiff * factor)
        self._place_end_labels()
        self._canvas.draw_idle()
