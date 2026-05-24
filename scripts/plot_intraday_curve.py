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
        time_sec, price, cum_volume, buy1, sell1 = line.split("\t")
        rows.append({
            "time_sec": int(time_sec),
            "price": float(price),
            "cum_volume": float(cum_volume),
            "buy1": None if buy1 == "\\N" else float(buy1),
            "sell1": None if sell1 == "\\N" else float(sell1),
        })
    return rows


def time_label(time_sec):
    hour = time_sec // 10000
    minute = (time_sec // 100) % 100
    second = time_sec % 100
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def scale(value, src_min, src_max, dst_min, dst_max):
    if math.isclose(src_min, src_max):
        return (dst_min + dst_max) / 2
    return dst_min + (value - src_min) * (dst_max - dst_min) / (src_max - src_min)


def polyline(points):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def volume_deltas(rows):
    deltas = []
    prev = None
    for row in rows:
        cur = row["cum_volume"]
        delta = 0 if prev is None else max(cur - prev, 0)
        deltas.append(delta)
        prev = cur
    return deltas


def make_svg(symbol, trade_date, rows):
    width = 1300
    height = 760
    left = 78
    right = 34
    top = 58
    price_bottom = 490
    volume_top = 545
    bottom = 705

    prices = [r["price"] for r in rows]
    price_min = min(prices)
    price_max = max(prices)
    padding = max((price_max - price_min) * 0.08, 0.001)
    price_min -= padding
    price_max += padding

    deltas = volume_deltas(rows)
    volume_max = max(deltas) if deltas else 1

    plot_w = width - left - right
    price_h = price_bottom - top
    volume_h = bottom - volume_top

    price_points = []
    buy_points = []
    sell_points = []
    for i, row in enumerate(rows):
        x = scale(i, 0, max(len(rows) - 1, 1), left, left + plot_w)
        y = scale(row["price"], price_min, price_max, price_bottom, top)
        price_points.append((x, y))
        if row["buy1"] is not None:
            buy_points.append((x, scale(row["buy1"], price_min, price_max, price_bottom, top)))
        if row["sell1"] is not None:
            sell_points.append((x, scale(row["sell1"], price_min, price_max, price_bottom, top)))

    bar_w = max(1.0, plot_w / max(len(rows), 1) * 0.7)
    bars = []
    for i, delta in enumerate(deltas):
        x = scale(i, 0, max(len(rows) - 1, 1), left, left + plot_w)
        bar_h = scale(delta, 0, volume_max, 0, volume_h)
        bars.append(
            f'<rect x="{x - bar_w / 2:.2f}" y="{bottom - bar_h:.2f}" '
            f'width="{bar_w:.2f}" height="{bar_h:.2f}" fill="#94a3b8" opacity="0.72" />'
        )

    grid = []
    price_labels = []
    for j in range(6):
        y = top + j * price_h / 5
        v = scale(y, price_bottom, top, price_min, price_max)
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e2e8f0" />')
        price_labels.append(f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="axis">{v:.3f}</text>')

    time_labels = []
    label_count = min(9, len(rows))
    for j in range(label_count):
        idx = round(j * (len(rows) - 1) / max(label_count - 1, 1))
        x = scale(idx, 0, max(len(rows) - 1, 1), left, left + plot_w)
        time_labels.append(
            f'<text x="{x:.2f}" y="{bottom + 28}" text-anchor="middle" class="axis">'
            f'{html.escape(time_label(rows[idx]["time_sec"]))}</text>'
        )

    first = rows[0]
    last = rows[-1]
    change = (last["price"] / first["price"] - 1) * 100 if first["price"] else 0

    buy_line = f'<polyline points="{polyline(buy_points)}" fill="none" stroke="#16a34a" stroke-width="1.2" opacity="0.45" />' if buy_points else ""
    sell_line = f'<polyline points="{polyline(sell_points)}" fill="none" stroke="#dc2626" stroke-width="1.2" opacity="0.45" />' if sell_points else ""

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <style>
    .title {{ font: 700 24px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #0f172a; }}
    .meta {{ font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #475569; }}
    .axis {{ font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #64748b; }}
    .legend {{ font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #334155; }}
  </style>
  <rect width="100%" height="100%" fill="#ffffff" />
  <text x="{left}" y="32" class="title">{html.escape(symbol)} intraday curve</text>
  <text x="{left}" y="53" class="meta">{html.escape(trade_date)} | {len(rows)} records | {html.escape(time_label(first["time_sec"]))} to {html.escape(time_label(last["time_sec"]))} | price {first["price"]:.3f} -> {last["price"]:.3f} ({change:+.2f}%)</text>
  <rect x="{left}" y="{top}" width="{plot_w}" height="{price_h}" fill="#f8fafc" stroke="#cbd5e1" />
  {''.join(grid)}
  {''.join(price_labels)}
  {buy_line}
  {sell_line}
  <polyline points="{polyline(price_points)}" fill="none" stroke="#2563eb" stroke-width="2.2" />
  <circle cx="{price_points[-1][0]:.2f}" cy="{price_points[-1][1]:.2f}" r="4" fill="#2563eb" />
  <text x="{left + plot_w}" y="{price_points[-1][1] - 8:.2f}" text-anchor="end" class="meta">{last["price"]:.3f}</text>
  <circle cx="{left + 14}" cy="{price_bottom + 25}" r="5" fill="#2563eb" /><text x="{left + 26}" y="{price_bottom + 29}" class="legend">avg_sell_price</text>
  <circle cx="{left + 162}" cy="{price_bottom + 25}" r="5" fill="#16a34a" opacity="0.55" /><text x="{left + 174}" y="{price_bottom + 29}" class="legend">buy1</text>
  <circle cx="{left + 228}" cy="{price_bottom + 25}" r="5" fill="#dc2626" opacity="0.55" /><text x="{left + 240}" y="{price_bottom + 29}" class="legend">sell1</text>
  <rect x="{left}" y="{volume_top}" width="{plot_w}" height="{volume_h}" fill="#f8fafc" stroke="#cbd5e1" />
  {''.join(bars)}
  <text x="{left}" y="{volume_top - 10}" class="meta">volume delta</text>
  {''.join(time_labels)}
</svg>
'''


def main():
    parser = argparse.ArgumentParser(description="Plot one trading day's intraday curve from ClickHouse market_data.")
    parser.add_argument("symbol", nargs="?", default="510500")
    parser.add_argument("date", nargs="?", default="20250410", help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--url", default="http://localhost:8123")
    parser.add_argument("--database", default="stock_db")
    parser.add_argument("--user", default="stock_user")
    parser.add_argument("--password", default="stock_pass")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    trade_date = args.date
    if len(trade_date) == 8 and trade_date.isdigit():
        trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    sql = f"""
SELECT
    time_sec,
    avg_sell_price,
    ifNull(cum_volume, 0),
    buy1_price,
    sell1_price
FROM market_data
WHERE code = '{args.symbol.replace("'", "''")}'
  AND trade_date = '{trade_date.replace("'", "''")}'
  AND avg_sell_price IS NOT NULL
ORDER BY time_sec
FORMAT TSV
"""
    text = query_clickhouse(args.url, args.user, args.password, args.database, sql)
    rows = parse_tsv(text)
    if not rows:
        raise SystemExit(f"No data found for {args.symbol} on {trade_date}")

    out = Path(args.output or f"{args.symbol}_{trade_date.replace('-', '')}_intraday.svg")
    out.write_text(make_svg(args.symbol, trade_date, rows), encoding="utf-8")

    print(f"symbol: {args.symbol}")
    print(f"date: {trade_date}")
    print(f"records: {len(rows)}")
    print(f"time: {time_label(rows[0]['time_sec'])} to {time_label(rows[-1]['time_sec'])}")
    print(f"price: {rows[0]['price']:.3f} -> {rows[-1]['price']:.3f}")
    print(f"output: {out}")


if __name__ == "__main__":
    main()
