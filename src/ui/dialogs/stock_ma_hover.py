"""ui/dialogs/stock_ma_hover.py — _HoverMixin: the crosshair of the MA chart
dialog (ui/dialogs/stock_ma.py StockMaDialog).

One crosshair shared by every panel, a dot + value badge on each visible
series at the cursor bar (_update_hover, _hover_series, _hover_text) and a
date badge on the bottom visible panel (_update_date_badge). _on_motion also
drives a left-drag pan started by _ViewMixin._on_press.

Dialog state it uses
--------------------
reads   _x, _dates, _lines, _panel_axes, _crosshairs, _fig, _canvas,
        _fmt_str, _pan_start; _col() (dialog), _visible_panels /
        _place_end_labels / _fit_margins (_PlotMixin), _chart_axes /
        _set_xlim (_ViewMixin)
writes  _cursor_idx, _hover, _date_badges

Moved out of ui/dialogs/stock_ma.py (2026-11-15); method names unchanged.
"""
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgb

from i18n import tr
from ui.dialogs.stock_ma_consts import _SERIES, _TICK_PT
from ui.dialogs.stock_ma_helpers import _compact
from ui.colors import PROFIT, LOSS
from ui.theme import ACCENT, TEXT, SURFACE, INK_ON_LIGHT



class _HoverMixin:
    """Crosshair, hover value badges and the date badge for StockMaDialog."""

    def _install_crosshair(self) -> None:
        canvas = self._canvas
        canvas.mpl_connect("motion_notify_event", self._on_motion)
        canvas.mpl_connect("axes_leave_event", self._on_leave)
        canvas.mpl_connect("figure_leave_event", self._on_leave)
        canvas.mpl_connect("resize_event", lambda _e: self._on_canvas_resize())

    def _on_canvas_resize(self) -> None:
        self._place_end_labels()
        self._fit_margins()

    def _index_at(self, xdata: float) -> int:
        i = int(np.searchsorted(self._x, xdata))
        if i <= 0:
            return 0
        if i >= len(self._x):
            return len(self._x) - 1
        return i if abs(self._x[i] - xdata) < abs(self._x[i - 1] - xdata) else i - 1

    def _set_cursor(self, idx) -> None:
        """Move the shared crosshair to bar `idx` (None hides it) together
        with its value and date badges."""
        self._cursor_idx = idx
        for line in self._crosshairs:
            if idx is None:
                line.set_visible(False)
            else:
                line.set_xdata([self._x[idx], self._x[idx]])
                line.set_visible(True)
        self._update_hover(idx)
        self._canvas.draw_idle()

    def _hover_text(self, key: str, v: float) -> str:
        if key in ("Div(20)", "Div(50)"):
            return tr(f"{key.replace('Div', '이격도')} {v:.1f}", f"{key} {v:.1f}")
        if key == "RSI":
            return f"RSI {v:.0f}"
        if key == "Vol":
            return tr(f"거래량 {_compact(v)}", f"Vol {_compact(v)}")
        if key == "IDX":
            return f"{self._market} {self._price_value_text(key, v, compact=False)}"
        prefix = "" if key == "Close" else f"{_SERIES.get(key, key)} "
        if key in self._price_raw:
            return prefix + self._price_value_text(key, v, compact=False)
        return prefix + self._fmt_str.format(v)

    def _hover_series(self, idx: int) -> list:
        """[(key, axes, y, color)] for every visible series at bar `idx`."""
        out = []
        for key, line in self._lines.items():
            if not (line.get_visible() and line.axes.get_visible()):
                continue
            y = line.get_ydata()[idx]
            if y is not None and np.isfinite(y):
                out.append((key, line.axes, float(y), line.get_color()))
        ax_vol = self._panel_axes.get("vol")
        vol = self._col("Volume")
        if ax_vol is not None and ax_vol.get_visible() and vol is not None:
            v = float(vol[idx])
            if np.isfinite(v):
                opens = self._col("Open")
                up = opens is None or self._col("Close")[idx] >= opens[idx]
                out.append(("Vol", ax_vol, v, PROFIT if up else LOSS))
        return out

    def _update_hover(self, idx) -> None:
        """A dot on every visible series at the cursor bar with its value next
        to it (user direction 2026-10-15), pushed apart per panel like the end
        labels; flipped to the left of the crosshair near the right edge."""
        shown = set()
        if idx is not None and self._x is not None:
            x = self._x[idx]
            xmin, xmax = self._panel_axes["price"].get_xlim()
            left = x > xmin + (xmax - xmin) * 0.8
            dpi = self._fig.dpi
            pitch_px = 1.6 * _TICK_PT * dpi / 72
            by_ax = {}
            for key, ax, y, color in self._hover_series(idx):
                by_ax.setdefault(ax, []).append((ax.transData.transform((x, y))[1], key, y, color))
            for ax, entries in by_ax.items():
                entries.sort(key=lambda t: t[0])
                placed = []
                for disp_y, key, y, color in entries:
                    if placed and disp_y - placed[-1][0] < pitch_px:
                        disp_y = placed[-1][0] + pitch_px
                    placed.append((disp_y, key, y, color))
                for disp_y, key, y, color in placed:
                    marker, ann = self._hover_artist(key, ax)
                    marker.set_data([x], [y])
                    marker.set_markerfacecolor(color)
                    anchor_px = ax.transData.transform((x, y))[1]
                    ann.xy = (x, y)
                    ann.set_position((-8 if left else 8, (disp_y - anchor_px) * 72 / dpi))
                    ann.set_horizontalalignment("right" if left else "left")
                    ann.set_text(self._hover_text(key, y))
                    r, g, b = to_rgb(color)
                    ann.set_color(INK_ON_LIGHT if 0.299 * r + 0.587 * g + 0.114 * b > 0.5 else TEXT)
                    ann.get_bbox_patch().set_facecolor(color)
                    marker.set_visible(True)
                    ann.set_visible(True)
                    shown.add(key)
        for key, (marker, ann) in self._hover.items():
            if key not in shown:
                marker.set_visible(False)
                ann.set_visible(False)
        self._update_date_badge(idx)

    def _update_date_badge(self, idx) -> None:
        """The cursor bar's date on the x axis of the bottom visible panel
        (user direction 2026-10-15, replacing the readout row's date)."""
        bottom = None
        if idx is not None and self._x is not None:
            visible = [k for k, _r in self._visible_panels()]
            bottom = visible[-1] if visible else None
        for key, ann in self._date_badges.items():
            ann.set_visible(key == bottom)
        if bottom is None:
            return
        if bottom not in self._date_badges:
            self._date_badges[bottom] = self._panel_axes[bottom].annotate(
                "", xy=(0, 0), xycoords=("data", "axes fraction"), xytext=(0, -3), textcoords="offset points",
                fontsize=_TICK_PT, color=INK_ON_LIGHT, ha="center", va="top", annotation_clip=False, zorder=10,
                bbox=dict(boxstyle="round,pad=0.3", facecolor=ACCENT, edgecolor="none"))
        ann = self._date_badges[bottom]
        ann.xy = (self._x[idx], 0)
        ann.set_text(pd.to_datetime(self._dates[idx]).strftime("%Y-%m-%d"))
        ann.set_visible(True)

    def _hover_artist(self, key: str, ax) -> tuple:
        """The (marker, label) pair for `key`, created on first use."""
        if key not in self._hover:
            marker, = ax.plot([np.nan], [np.nan], marker="o", markersize=6, linestyle="none",
                              markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=8, visible=False)
            ann = ax.annotate("", xy=(0, 0), xytext=(8, 0), textcoords="offset points",
                              fontsize=_TICK_PT, va="center", annotation_clip=False, zorder=9, visible=False,
                              bbox=dict(boxstyle="round,pad=0.25", edgecolor="none", alpha=0.92))
            self._hover[key] = (marker, ann)
        return self._hover[key]

    def _on_leave(self, _event) -> None:
        if self._cursor_idx is not None:
            self._set_cursor(None)

    def _on_motion(self, event) -> None:
        if self._pan_start is not None:
            xpress, xlim = self._pan_start
            bbox = self._panel_axes["price"].get_window_extent()
            if bbox.width == 0:
                return
            dx_data = (event.x - xpress) * ((xlim[1] - xlim[0]) / bbox.width)
            self._set_xlim(xlim[0] - dx_data, xlim[1] - dx_data)
            return
        if event.inaxes not in self._chart_axes() or event.xdata is None:
            if self._cursor_idx is not None:
                self._set_cursor(None)
            return
        idx = self._index_at(event.xdata)
        if idx != self._cursor_idx:
            self._set_cursor(idx)
