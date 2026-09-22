"""Draw the flood-coverage choropleth from the country pass.

Reads `data/country_rows.csv` -- the flat (article, country) fact table --
rather than the pre-aggregated totals, because every useful question about this
map is a filter on those rows: one outlet's beat, one year, primary subjects
only. Aggregation happens here, after filtering, so the counts always match
what is drawn.

    python3 map_countries.py                          # everything, log scale
    python3 map_countries.py --primary-only           # drop secondary mentions
    python3 map_countries.py --exclude-outlet floodlist
    python3 map_countries.py --year-from 2024 --year-to 2026
    python3 map_countries.py --serve                  # write, then serve it

Every run writes the SAME file, `data/map_view/flood_map.html`. Filters change
what the map shows, not where it lands.

VIEWING IT FROM A LAPTOP. The box is headless, so either copy the file down:

    scp <this-host>:/home/liu47/vlm_flood/data/map_view/flood_map.html .

or serve it and tunnel, which is what `--serve` prints instructions for:

    python3 map_countries.py --serve            # on the server
    ssh -N -L 8767:localhost:8767 <this-host>   # on the laptop
    # then open http://localhost:8767/flood_map.html

The server binds 127.0.0.1, never 0.0.0.0: this machine is shared. The tunnel
is what makes it reachable, and only by you.

Output is an interactive HTML (hover, zoom, pan) that opens in any browser
with no server. plotly.js is embedded by default -- ~4.8 MB instead of 20 KB,
the right trade for a file you scp off a headless box -- and `--cdn` writes the
small version that loads the library from the network instead.

It is NOT fully offline either way: plotly draws choropleth country outlines
from topojson it fetches from cdn.plot.ly when the page renders. The library is
local, the geometry is not, so the first open needs internet. A truly airtight
file means bundling the topojson and overriding `topojsonURL`; not done here
because nothing in this workflow opens the map without a network.

TWO DELIBERATE CHOICES, both of which a default plotly call gets wrong here:

1. LOG COLOUR SCALE. The range is 1089 (US) to 1, three orders of magnitude.
   On a linear scale the US is dark, Australia and the UK are faint, and the
   other 178 countries are indistinguishable from empty -- the map reads as if
   it has no data. Colour is log10(articles) with the colourbar relabelled in
   real article counts, so the eye sees the spread and reads true numbers.
   `--linear` restores the naive version for comparison.

2. THE TITLE SAYS "COVERAGE". USA 1089, AUS 816, GBR 500, NZL 225 top this
   list substantially because the outlet list is English-language. Bangladesh
   at 81 does not flood less than New Zealand. This is a map of where the
   corpus REPORTS floods, and the title says so rather than letting the
   picture imply flood incidence.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: Every run writes THIS file. The filters are how you ask a different
#: question, not a reason to keep a second map around: two HTML files named
#: after flags go stale the moment the data is re-compiled, and there is no way
#: to tell from the file which cut it holds. The scope is written into the
#: title inside the map instead, so the picture always says what it is.
DEFAULT_OUTPUT = ROOT / "data" / "map_view" / "flood_map.html"

#: 8765 and 8766 are the review sites and 8767 was already taken by another
#: user when this was written -- the box is shared, so the port is a guess and
#: `--serve <port>` is the fix, not a bug.
DEFAULT_PORT = 8768

#: Colourbar stops, in real article counts. Chosen as a 1-3-10 ladder so the
#: ticks stay evenly spaced once log10 is applied.
TICKS = [1, 3, 10, 30, 100, 300, 1000]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Choropleth of flood coverage by country.")
    parser.add_argument("--rows", type=Path, default=ROOT / "data" / "country_rows.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Escape hatch. The default is a FIXED path, so every "
                             "run overwrites one file instead of littering.")
    parser.add_argument("--primary-only", action="store_true",
                        help="Keep only each article's subject country (mention_rank 0).")
    parser.add_argument("--exclude-outlet", action="append", default=[],
                        help="Drop an outlet; repeatable. floodlist is 44%% of the corpus.")
    parser.add_argument("--only-outlet", action="append", default=[],
                        help="Keep only these outlets; repeatable.")
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument("--linear", action="store_true",
                        help="Linear colour scale instead of log (see module docstring).")
    parser.add_argument("--projection", default="natural earth",
                        help="plotly projection: 'natural earth', 'robinson', 'equirectangular'.")
    parser.add_argument("--cdn", action="store_true",
                        help="Load plotly.js from the CDN instead of embedding it: "
                             "20 KB rather than ~4 MB, but needs internet to open.")
    parser.add_argument("--serve", type=int, nargs="?", const=DEFAULT_PORT, default=None,
                        help="After writing, serve the output directory on this port "
                             f"(default {DEFAULT_PORT}) bound to localhost. Reach it from your "
                             "laptop with an SSH tunnel -- see the module docstring.")
    parser.add_argument("--png", type=Path, default=None,
                        help="Also write a static PNG (needs `pip install kaleido`).")
    return parser.parse_args()


def serve(output: Path, port: int) -> None:
    """Serve the map's directory on localhost until interrupted.

    Bound to 127.0.0.1 on purpose: this box is shared, and a map served on
    0.0.0.0 is a map every other user can read. Reaching it from a laptop is
    an SSH tunnel, not a wider bind -- the same shape as the :8765 and :8766
    review sites.
    """
    import functools
    import http.server
    import socketserver

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(output.parent)
    )
    socketserver.TCPServer.allow_reuse_address = True
    try:
        server = socketserver.TCPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        # Shared box: someone else's service, or your own from a past run.
        raise SystemExit(
            f"Port {port} is not available ({exc}). The map is already written to "
            f"{output} -- pick another port with --serve <port>, or scp the file down."
        ) from exc
    with server:
        print(f"\nServing {output.parent} on http://127.0.0.1:{port}/")
        print(f"  From your laptop:  ssh -N -L {port}:localhost:{port} "
              f"{os.environ.get('USER', 'user')}@<this-host>")
        print(f"  Then open:         http://localhost:{port}/{output.name}")
        print("  Ctrl-C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> int:
    args = parse_args()
    import numpy as np
    import pandas as pd
    import plotly.express as px

    if not args.rows.exists():
        raise SystemExit(
            f"{args.rows} does not exist. Build it first:\n"
            f"    python3 verification/compile_countries.py"
        )
    frame = pd.read_csv(args.rows)
    before = len(frame)

    # -- filters, applied before aggregation ------------------------------
    if args.primary_only:
        frame = frame[frame["mention_rank"] == 0]
    if args.exclude_outlet:
        frame = frame[~frame["outlet"].isin(args.exclude_outlet)]
    if args.only_outlet:
        frame = frame[frame["outlet"].isin(args.only_outlet)]
    if args.year_from is not None:
        frame = frame[pd.to_numeric(frame["year"], errors="coerce") >= args.year_from]
    if args.year_to is not None:
        frame = frame[pd.to_numeric(frame["year"], errors="coerce") <= args.year_to]

    # Unresolved rows (Kosovo -- ISO 3166-1 has no code) carry no alpha_3 and
    # cannot be drawn. Dropped loudly rather than silently.
    unplottable = frame["alpha_3"].isna().sum()
    frame = frame[frame["alpha_3"].notna()]
    if frame.empty:
        raise SystemExit("No rows left after filtering -- nothing to draw.")

    counts = (frame.groupby(["alpha_3", "country_name"])
                   .agg(articles=("article_url", "size"),
                        primary=("mention_rank", lambda s: int((s == 0).sum())),
                        secondary=("mention_rank", lambda s: int((s > 0).sum())))
                   .reset_index())

    print(f"rows {before} -> {len(frame)} after filters"
          + (f"  ({unplottable} unresolved dropped)" if unplottable else ""))
    print(f"countries: {len(counts)}   mentions: {int(counts['articles'].sum())}")

    # -- the map ----------------------------------------------------------
    counts["colour"] = counts["articles"] if args.linear else np.log10(counts["articles"])
    scope = []
    if args.primary_only:
        scope.append("subject country only")
    if args.exclude_outlet:
        scope.append("excl. " + ", ".join(args.exclude_outlet))
    if args.only_outlet:
        scope.append("only " + ", ".join(args.only_outlet))
    if args.year_from or args.year_to:
        scope.append(f"{args.year_from or 'start'}-{args.year_to or 'end'}")
    subtitle = f" ({'; '.join(scope)})" if scope else ""

    figure = px.choropleth(
        counts,
        locations="alpha_3",          # locationmode defaults to ISO-3
        color="colour",
        hover_name="country_name",
        hover_data={"articles": True, "primary": True, "secondary": True,
                    "alpha_3": False, "colour": False},
        color_continuous_scale="Blues",
        projection=args.projection,
        title=f"Flood coverage in the news corpus{subtitle}"
              f" — {int(counts['articles'].sum())} article mentions, "
              f"{len(counts)} countries",
    )
    if args.linear:
        figure.update_coloraxes(colorbar=dict(title="articles"))
    else:
        shown = [t for t in TICKS if t <= counts["articles"].max() * 1.5]
        figure.update_coloraxes(colorbar=dict(
            title="articles",
            tickvals=[float(np.log10(t)) for t in shown],
            ticktext=[str(t) for t in shown],
        ))
    figure.update_geos(showcountries=True, countrycolor="white",
                       showcoastlines=False, landcolor="#f2f2f2")
    figure.update_layout(margin=dict(l=0, r=0, t=60, b=0))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(args.output, include_plotlyjs="cdn" if args.cdn else True)
    size_mb = args.output.stat().st_size / 1e6
    library = "plotly.js from CDN" if args.cdn else "plotly.js embedded"
    print(f"\nWrote {args.output}  ({size_mb:.1f} MB, {library}; "
          f"country outlines still load from cdn.plot.ly on open)")
    if args.png:
        try:
            figure.write_image(args.png, width=1600, height=900, scale=2)
            print(f"Wrote {args.png}")
        except Exception as exc:  # noqa: BLE001 -- kaleido is optional
            print(f"PNG skipped ({type(exc).__name__}: {exc}). "
                  f"Install it with: pip install kaleido", file=sys.stderr)

    if args.serve:
        serve(args.output, args.serve)

    print("\nTop 10:")
    for row in counts.nlargest(10, "articles").itertuples():
        print(f"  {row.alpha_3}  {row.articles:5d}  {row.country_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
