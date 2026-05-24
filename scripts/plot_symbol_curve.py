#!/usr/bin/env python3
import argparse
import html
import math
import urllib.parse
import urllib.request
from pathlib import Path


def query_clickhouse(url, user, password, database, sql):
    params = urllib.parse.urlencode({
        "user": user,
        "password": password,
        "database": database,
    })
    req = urllib.request.Request(
        f"{url.rstrip('/')}?{params}",
        data=sql.encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_tsv(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        date, open_, high, low, close, volume, amount, ticks = line.split("\t")
        rows.append({
            "date": date,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
            "amount": float(amount),
            "ticks": int(ticks),
        })
    return rows


def scale(value, src_min, src_max, dst_min, dst_max):
    if math.isclose(src_min, src_max):
        return (dst_min + dst_max) / 2
    return dst_min + (value - src_min) * (dst_max - dst_min) / (src_max - src_min)


def polyline(points):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def make_svg(symbol, rows):
    width = 1200
    height = 720
    left = 72
    right = 32
    top = 52
    price_bottom = 455
    volume_top = 500
    bottom = 668

    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    price_min = min(r["low"] for r in rows)
    price_max = max(r["high"] for r in rows)
    volume_max = max(volumes) if volumes else 1

    plot_w = width - left - right
    price_h = price_bottom - top
    volume_h = bottom - volume_top

    price_points = []
    for i, row in enumerate(rows):
        x = scale(i, 0, max(len(rows) - 1, 1), left, left + plot_w)
        y = scale(row["close"], price_min, price_max, price_bottom, top)
        price_points.append((x, y))

    volume_bars = []
    bar_w = max(1.0, plot_w / max(len(rows), 1) * 0.65)
    for i, row in enumerate(rows):
        x = scale(i, 0, max(len(rows) - 1, 1), left, left + plot_w)
        bar_h = scale(row["volume"], 0, volume_max, 0, volume_h)
        volume_bars.append(
            f'<rect x="{x - bar_w / 2:.2f}" y="{bottom - bar_h:.2f}" '
            f'width="{bar_w:.2f}" height="{bar_h:.2f}" fill="#94a3b8" opacity="0.75" />'
        )

    grid = []
    labels = []
    for j in range(5):
        y = top + j * price_h / 4
        v = scale(y, price_bottom, top, price_min, price_max)
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e2e8f0" />')
        labels.append(f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="axis">{v:.3f}</text>')

    date_labels = []
    label_count = min(8, len(rows))
    if label_count:
        for j in range(label_count):
            idx = round(j * (len(rows) - 1) / max(label_count - 1, 1))
            x = scale(idx, 0, max(len(rows) - 1, 1), left, left + plot_w)
            date_labels.append(
                f'<text x="{x:.2f}" y="{bottom + 28}" text-anchor="middle" class="axis">'
                f'{html.escape(rows[idx]["date"])}</text>'
            )

    first = rows[0]
    last = rows[-1]
    change = (last["close"] / first["close"] - 1) * 100 if first["close"] else 0

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <style>
    .title {{ font: 700 24px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #0f172a; }}
    .meta {{ font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #475569; }}
    .axis {{ font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #64748b; }}
  </style>
  <rect width="100%" height="100%" fill="#ffffff" />
  <text x="{left}" y="32" class="title">{html.escape(symbol)} daily curve</text>
  <text x="{left}" y="52" class="meta">{html.escape(first["date"])} to {html.escape(last["date"])} | {len(rows)} trading days | close {first["close"]:.3f} -> {last["close"]:.3f} ({change:+.2f}%)</text>
  <rect x="{left}" y="{top}" width="{plot_w}" height="{price_h}" fill="#f8fafc" stroke="#cbd5e1" />
  {''.join(grid)}
  {''.join(labels)}
  <polyline points="{polyline(price_points)}" fill="none" stroke="#2563eb" stroke-width="2.5" />
  <circle cx="{price_points[-1][0]:.2f}" cy="{price_points[-1][1]:.2f}" r="4" fill="#2563eb" />
  <text x="{left + plot_w}" y="{price_points[-1][1] - 8:.2f}" text-anchor="end" class="meta">{last["close"]:.3f}</text>
  <rect x="{left}" y="{volume_top}" width="{plot_w}" height="{volume_h}" fill="#f8fafc" stroke="#cbd5e1" />
  {''.join(volume_bars)}
  <text x="{left}" y="{volume_top - 10}" class="meta">volume</text>
  {''.join(date_labels)}
</svg>
'''


def main():
    parser = argparse.ArgumentParser(description="Plot daily curve from ClickHouse market_data.")
    parser.add_argument("symbol", nargs="?", default="510500")
    parser.add_argument("--url", default="http://localhost:8123")
    parser.add_argument("--database", default="stock_db")
    parser.add_argument("--user", default="stock_user")
    parser.add_argument("--password", default="stock_pass")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    sql = f"""
SELECT
    toString(trade_date) AS d,
    argMin(avg_sell_price, time_sec) AS open,
    max(ifNull(high_price, avg_sell_price)) AS high,
    min(ifNull(low_price, avg_sell_price)) AS low,
    argMax(avg_sell_price, time_sec) AS close,
    argMax(ifNull(cum_volume, 0), time_sec) AS volume,
    argMax(ifNull(cum_amount, 0), time_sec) AS amount,
    count() AS ticks
FROM market_data
WHERE code = '{args.symbol.replace("'", "''")}'
  AND avg_sell_price IS NOT NULL
GROUP BY trade_date
ORDER BY trade_date
FORMAT TSV
"""
    text = query_clickhouse(args.url, args.user, args.password, args.database, sql)
    rows = parse_tsv(text)
    if not rows:
        raise SystemExit(f"No data found for {args.symbol}")

    out = Path(args.output or f"{args.symbol}_daily_curve.svg")
    out.write_text(make_svg(args.symbol, rows), encoding="utf-8")

    print(f"symbol: {args.symbol}")
    print(f"days: {len(rows)}")
    print(f"range: {rows[0]['date']} to {rows[-1]['date']}")
    print(f"latest close: {rows[-1]['close']:.3f}")
    print(f"output: {out}")


if __name__ == "__main__":
    main()
