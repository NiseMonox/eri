"""体重曲线 PNG(matplotlib Agg)。配色遵循 dataviz 规范浅色面:单序列蓝,无图例,细网格。"""

import io
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
from matplotlib import dates as mdates  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402

from . import clock  # noqa: E402

SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
GRID = "#e1e0d9"
MUTED = "#898781"
INK = "#0b0b0b"
SECONDARY = "#52514e"
BASELINE = "#c3c2b7"

_font_ready = False


def _ensure_cjk_font() -> None:
    global _font_ready
    if _font_ready:
        return
    for name in ("Noto Sans CJK JP", "Noto Sans CJK SC", "Noto Sans JP"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            break
        except Exception:  # noqa: BLE001
            continue
    plt.rcParams["axes.unicode_minus"] = False
    _font_ready = True


def weight_chart_png(days: int = 30) -> bytes:
    from .services import weights

    _ensure_cjk_font()
    rows = weights.recent_days(days)

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    if rows:
        xs = [clock.to_local(clock.parse_iso(r["measured_at"])) for r in rows]
        ys = [r["weight_kg"] for r in rows]
        ax.plot(xs, ys, color=SERIES, linewidth=2, marker="o", markersize=5, zorder=3)
        # 只直标最新一点,不逐点标数
        ax.annotate(
            f"{ys[-1]:.1f}",
            (xs[-1], ys[-1]),
            textcoords="offset points",
            xytext=(8, 4),
            color=INK,
            fontsize=11,
            fontweight="bold",
        )
        end = clock.now_local()
        ax.set_xlim(end - timedelta(days=days), end + timedelta(hours=12))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=clock.TOKYO))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d", tz=clock.TOKYO))
        pad = max((max(ys) - min(ys)) * 0.15, 0.5)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
    else:
        ax.text(0.5, 0.5, "データなし", transform=ax.transAxes, ha="center", va="center",
                color=MUTED, fontsize=14)
        ax.set_xticks([])
        ax.set_yticks([])

    ax.set_title(f"体重(直近{days}日)", color=INK, fontsize=13, loc="left", pad=12)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.set_ylabel("kg", color=SECONDARY, fontsize=9, rotation=0, labelpad=16)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
