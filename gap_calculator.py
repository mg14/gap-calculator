#!/usr/bin/env python3
"""
GPX GAP (Grade Adjusted Pace) Calculator

Applies the Strava GAP algorithm to compute expected per-km splits
and total time at a given target Grade Adjusted Pace.

Formula:
    GAP_speed = actual_speed * gap_speed_factor(grade)
    → actual_speed = target_GAP_speed / gap_speed_factor(grade)

Usage:
    python gap_calculator.py activity.gpx
    python gap_calculator.py activity.gpx --gap 5.5
    python gap_calculator.py activity.gpx --gap 6.0 --smooth 20
    python gap_calculator.py activity.gpx --splits 5          # every 5 km
    python gap_calculator.py activity.gpx --splits 5 10 21.1  # explicit distances

No third-party dependencies required.
"""

import argparse
import bisect
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Strava GAP speed-factor (inlined from github.com/aaron-schroeder/specialsauce,
# MIT licence — reverse-engineered by uploading spoofed Strava activity files)
# ---------------------------------------------------------------------------

# Lookup table: grade % → GAP speed factor
# Factor > 1 means GAP is faster than actual speed (uphill effort).
# Factor < 1 means GAP is slower than actual speed (gentle downhill).
_GAP_GRADES  = [-45, -30, -25, -20, -15, -10, -8, -6, -4, -2,
                  0,   2,   4,   6,   8,  10,  15, 20, 25, 30, 45]
_GAP_FACTORS = [2.096, 1.495, 1.273, 1.081, 0.941, 0.876, 0.876, 0.891,
                0.918, 0.96, 1.0, 1.055, 1.135, 1.228, 1.337, 1.459,
                1.846, 2.297, 2.727, 3.158, 4.286]


def gap_speed_factor(decimal_grade):
    """Return Strava's GAP speed-factor for the given decimal grade.

    GAP_speed = actual_speed * gap_speed_factor(grade)
    Grade is clamped to [-0.45, 0.45] (range of the original investigation).
    Uses piecewise-linear interpolation over the empirical lookup table.
    """
    grade_pct = max(-45.0, min(45.0, decimal_grade * 100))
    i = bisect.bisect_left(_GAP_GRADES, grade_pct)
    if i == 0:
        return _GAP_FACTORS[0]
    if i >= len(_GAP_GRADES):
        return _GAP_FACTORS[-1]
    g0, g1 = _GAP_GRADES[i - 1], _GAP_GRADES[i]
    f0, f1 = _GAP_FACTORS[i - 1], _GAP_FACTORS[i]
    return f0 + (grade_pct - g0) / (g1 - g0) * (f1 - f0)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    """Horizontal distance in metres between two WGS-84 coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------

def parse_gpx(path):
    """Return [(lat, lon, elevation_m), ...] for every track point."""
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    pts = []
    for tp in root.findall(".//g:trkpt", ns):
        lat = float(tp.get("lat"))
        lon = float(tp.get("lon"))
        el = tp.find("g:ele", ns)
        ele = float(el.text) if el is not None else 0.0
        pts.append((lat, lon, ele))
    if not pts:
        sys.exit("No track points found in GPX file.")
    return pts


def parse_gpx_with_times(path):
    """Return [(lat, lon, elevation_m, time_str_or_None), ...] for every track point."""
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    pts = []
    for tp in root.findall(".//g:trkpt", ns):
        lat = float(tp.get("lat"))
        lon = float(tp.get("lon"))
        el = tp.find("g:ele", ns)
        ele = float(el.text) if el is not None else 0.0
        tm = tp.find("g:time", ns)
        pts.append((lat, lon, ele, tm.text if tm is not None else None))
    if not pts:
        sys.exit("No track points found in GPX file.")
    return pts


# ---------------------------------------------------------------------------
# Recorded pace from GPX timestamps
# ---------------------------------------------------------------------------

def parse_recorded_times(pts_full):
    """Return cumulative recorded elapsed time (s) aligned with build_profile's arrays.

    Applies the same d < 0.1 skip logic so the returned array has the same
    length as cum_dist / cum_time from build_profile.
    Returns None when no timestamps are found in the file.
    """
    lats = [p[0] for p in pts_full]
    lons = [p[1] for p in pts_full]

    dts = []
    for _, _, _, t_str in pts_full:
        if t_str:
            s = t_str.rstrip("Z").split(".")[0]
            try:
                dts.append(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                           .replace(tzinfo=timezone.utc))
            except ValueError:
                dts.append(None)
        else:
            dts.append(None)

    if not any(dt is not None for dt in dts):
        return None

    first_dt = next(dt for dt in dts if dt is not None)

    cum_rec = [0.0]
    for i in range(1, len(pts_full)):
        d = haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
        if d < 0.1:
            continue
        if dts[i] is not None:
            cum_rec.append((dts[i] - first_dt).total_seconds())
        else:
            cum_rec.append(cum_rec[-1])   # hold last known time

    return cum_rec


# ---------------------------------------------------------------------------
# Elevation smoothing
# ---------------------------------------------------------------------------

def smooth_elevation(values, sigma, distances=None):
    """Gaussian kernel smoothing.

    If `distances` (cumulative metres, same length as `values`) is given,
    `sigma` is the kernel standard deviation in metres — correct for non-uniform
    GPS point spacing.  Otherwise `sigma` is in array-index units (used for
    pace-array smoothing where uniform spacing is assumed).

    Kernel is truncated at ±3σ; bisect is used to find the window bounds
    efficiently when distances are provided.
    """
    n = len(values)
    result = []
    if distances is not None:
        sigma = max(1.0, sigma)
        cutoff = 3.0 * sigma
        for i in range(n):
            d_i = distances[i]
            lo = bisect.bisect_left(distances, d_i - cutoff)
            hi = bisect.bisect_right(distances, d_i + cutoff)
            total_w = total_v = 0.0
            for j in range(lo, hi):
                w = math.exp(-0.5 * ((distances[j] - d_i) / sigma) ** 2)
                total_w += w
                total_v += w * values[j]
            result.append(total_v / total_w)
    else:
        sigma = max(1.0, sigma)
        radius = int(math.ceil(3.0 * sigma))
        for i in range(n):
            lo = max(0, i - radius)
            hi = min(n, i + radius + 1)
            total_w = total_v = 0.0
            for j in range(lo, hi):
                w = math.exp(-0.5 * ((j - i) / sigma) ** 2)
                total_w += w
                total_v += w * values[j]
            result.append(total_v / total_w)
    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_time(seconds):
    """Format seconds as [H:]MM:SS."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_pace(seconds_per_km):
    """Format seconds/km as M:SS /km."""
    s = int(round(seconds_per_km))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d} /km"


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def build_profile(points, start_gap_min_km, smooth_sigma_m, end_gap_min_km=None):
    """
    Walk every track segment and return parallel arrays indexed by track point:
        cum_dist  – cumulative horizontal distance (m)
        cum_time  – cumulative elapsed time (s)
        cum_ele   – smoothed elevation (m)
        cum_lats  – latitude
        cum_lons  – longitude
        cum_pace  – actual running pace (min/km) derived from grade + local GAP

    If end_gap_min_km is given the target GAP increases linearly from
    start_gap_min_km at the start to end_gap_min_km at the finish.
    smooth_sigma_m is the Gaussian kernel sigma in metres.
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]

    # Pre-compute cumulative horizontal distances so the Gaussian kernel
    # uses real spatial separation rather than point indices.
    cum_horiz = [0.0]
    for i in range(1, len(points)):
        cum_horiz.append(cum_horiz[-1] + haversine(lats[i - 1], lons[i - 1], lats[i], lons[i]))

    eles = smooth_elevation([p[2] for p in points], smooth_sigma_m, distances=cum_horiz)

    total_horiz = cum_horiz[-1] if end_gap_min_km is not None else None

    cum_dist = [0.0]
    cum_time = [0.0]
    cum_ele  = [eles[0]]
    cum_lats = [lats[0]]
    cum_lons = [lons[0]]
    cum_pace = [start_gap_min_km]   # actual pace at start = target GAP on flat

    for i in range(1, len(points)):
        d = haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
        if d < 0.1:          # skip GPS jitter (< 10 cm)
            continue
        grade = max(-0.45, min(0.45, (eles[i] - eles[i - 1]) / d))

        # Local target GAP (constant or linearly interpolated)
        if end_gap_min_km is not None and total_horiz > 0:
            f = cum_dist[-1] / total_horiz
            local_gap = start_gap_min_km + f * (end_gap_min_km - start_gap_min_km)
        else:
            local_gap = start_gap_min_km

        gap_speed_ms  = 1000.0 / (local_gap * 60)
        actual_speed  = gap_speed_ms / gap_speed_factor(grade)
        actual_pace   = 1000.0 / (actual_speed * 60)   # min/km

        cum_dist.append(cum_dist[-1] + d)
        cum_time.append(cum_time[-1] + d / actual_speed)
        cum_ele.append(eles[i])
        cum_lats.append(lats[i])
        cum_lons.append(lons[i])
        cum_pace.append(actual_pace)

    return cum_dist, cum_time, cum_ele, cum_lats, cum_lons, cum_pace


def _interp(cum_dist, values, target_d):
    """Linearly interpolate a value array at cumulative distance target_d."""
    for j in range(1, len(cum_dist)):
        if cum_dist[j] >= target_d:
            d0, d1 = cum_dist[j - 1], cum_dist[j]
            v0, v1 = values[j - 1], values[j]
            f = (target_d - d0) / (d1 - d0) if d1 > d0 else 0.0
            return v0 + f * (v1 - v0)
    return values[-1]


def interp_time(cum_dist, cum_time, target_d):
    return _interp(cum_dist, cum_time, target_d)


def interp_ele(cum_dist, cum_ele, target_d):
    return _interp(cum_dist, cum_ele, target_d)


def elevation_stats(cum_dist, cum_ele, start_d, end_d):
    """Total ascent (+m) and total descent (-m) between two cumulative distances."""
    gain = loss = 0.0
    prev = _interp(cum_dist, cum_ele, start_d)
    for d, e in zip(cum_dist, cum_ele):
        if d <= start_d:
            continue
        if d > end_d:
            break
        delta = e - prev
        if delta > 0:
            gain += delta
        else:
            loss += delta
        prev = e
    delta = _interp(cum_dist, cum_ele, end_d) - prev
    if delta > 0:
        gain += delta
    else:
        loss += delta
    return gain, loss


# ---------------------------------------------------------------------------
# Virtual training partner
# ---------------------------------------------------------------------------

def compute_point_times(points, start_gap_min_km, smooth_sigma_m, end_gap_min_km=None):
    """Return elapsed time in seconds for every point in `points`.

    Mirrors build_profile's logic exactly so the timestamps are consistent
    with the printed split table. Points skipped due to GPS jitter (d < 0.1 m)
    receive the same timestamp as the preceding point.
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]

    cum_horiz = [0.0]
    for i in range(1, len(points)):
        cum_horiz.append(cum_horiz[-1] + haversine(lats[i - 1], lons[i - 1], lats[i], lons[i]))

    eles = smooth_elevation([p[2] for p in points], smooth_sigma_m, distances=cum_horiz)

    total_horiz = cum_horiz[-1] if end_gap_min_km is not None else None

    elapsed = [0.0]
    cum_d = cum_t = 0.0

    for i in range(1, len(points)):
        d = haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
        if d < 0.1:
            elapsed.append(cum_t)
            continue
        grade = max(-0.45, min(0.45, (eles[i] - eles[i - 1]) / d))
        if end_gap_min_km is not None and total_horiz:
            f = cum_d / total_horiz
            local_gap = start_gap_min_km + f * (end_gap_min_km - start_gap_min_km)
        else:
            local_gap = start_gap_min_km
        gap_speed_ms = 1000.0 / (local_gap * 60)
        actual_speed = gap_speed_ms / gap_speed_factor(grade)
        cum_d += d
        cum_t += d / actual_speed
        elapsed.append(cum_t)

    return elapsed


def write_virtual_gpx(points_with_time, elapsed, output_path=None,
                      start_gap_min_km=6.0, end_gap_min_km=None):
    """Write a GPX file whose timestamps encode the GAP-derived pace plan.

    The start time is taken from the first timestamp in the source GPX so the
    file aligns with the original activity on Garmin/Strava/etc.
    If no timestamps are present a UTC epoch reference is used.
    Original lat/lon/elevation values are preserved unchanged.
    """
    # Determine start datetime
    start_str = next((t for _, _, _, t in points_with_time if t), None)
    if start_str:
        s = start_str.rstrip("Z").split(".")[0]          # strip tz/ms
        start_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        start_dt = datetime(2000, 1, 1, tzinfo=timezone.utc)

    gap_label = fmt_pace(start_gap_min_km * 60)
    if end_gap_min_km:
        gap_label += f" \u2192 {fmt_pace(end_gap_min_km * 60)}"

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="gap_calculator"',
        '  xmlns="http://www.topografix.com/GPX/1/1"',
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '  xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/11.xsd">',
        '  <metadata>',
        f'    <name>Virtual Partner \u2013 GAP {gap_label}</name>',
        f'    <time>{iso(start_dt)}</time>',
        '  </metadata>',
        '  <trk>',
        f'    <name>Virtual Partner \u2013 GAP {gap_label}</name>',
        '    <type>running</type>',
        '    <trkseg>',
    ]

    for (lat, lon, ele, _), secs in zip(points_with_time, elapsed):
        ts = iso(start_dt + timedelta(seconds=secs))
        lines += [
            f'      <trkpt lat="{lat:.8f}" lon="{lon:.8f}">',
            f'        <ele>{ele:.1f}</ele>',
            f'        <time>{ts}</time>',
            '      </trkpt>',
        ]

    lines += ['    </trkseg>', '  </trk>', '</gpx>']

    content = "\n".join(lines) + "\n"
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return content


# ---------------------------------------------------------------------------
# Split points
# ---------------------------------------------------------------------------

def make_split_points(splits_arg, total_dist_m):
    """
    Convert the --splits argument to a list of (label, distance_m) tuples.

    Single value  → repeat every N km until total distance is covered.
    Multiple values → treat each as an explicit cumulative distance in km.
    Points beyond the total route distance are silently dropped.
    """
    if len(splits_arg) == 1:
        interval_m = splits_arg[0] * 1000
        pts, n = [], 1
        while True:
            d = interval_m * n
            if d > total_dist_m + 1:
                break
            pts.append((f"{splits_arg[0] * n:g}", d))
            n += 1
        return pts
    else:
        return [
            (f"{s:g}", s * 1000)
            for s in sorted(splits_arg)
            if s * 1000 <= total_dist_m + 100   # 100 m tolerance for rounding
        ]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_grade(grade_pct):
    """Format a gradient percentage with sign, e.g. '+8.3%' or '-4.1%'."""
    sign = "+" if grade_pct >= 0 else ""
    return f"{sign}{grade_pct:.1f}%"


def print_results(args, cum_dist, cum_time, cum_ele, split_points, end_gap=None):
    total_dist = cum_dist[-1]
    total_time = cum_time[-1]

    COL_W = (14, 9, 12, 10, 10, 7, 7)
    SEP = "  "

    def row(*fields):
        return SEP.join(f"{str(f):>{w}}" for f, w in zip(fields, COL_W))

    print()
    gap_label = (
        f"{fmt_pace(args.gap * 60)} → {fmt_pace(end_gap * 60)}"
        if end_gap else fmt_pace(args.gap * 60)
    )
    print(f"File:           {args.gpx_file}")
    print(f"Target GAP:     {gap_label}")
    print(f"Total distance: {total_dist / 1000:.2f} km")
    print()

    header = row("km", "Split", "Pace", "Elapsed", "Avg Grade", "Gain", "Loss")
    divider = "-" * len(header)
    print(header)
    print(divider)

    prev_dist = 0.0
    prev_time = 0.0
    prev_ele  = cum_ele[0]
    total_gain = total_loss = 0.0

    for label, dist_m in split_points:
        dist_m    = min(dist_m, total_dist)
        t         = interp_time(cum_dist, cum_time, dist_m)
        ele       = interp_ele(cum_dist, cum_ele, dist_m)
        split     = t - prev_time
        seg_d     = dist_m - prev_dist
        pace      = split / (seg_d / 1000) if seg_d > 0 else 0.0
        grade_pct = (ele - prev_ele) / seg_d * 100 if seg_d > 0 else 0.0
        gain, loss = elevation_stats(cum_dist, cum_ele, prev_dist, dist_m)
        total_gain += gain
        total_loss += loss
        print(row(label, fmt_time(split), fmt_pace(pace), fmt_time(t),
                  fmt_grade(grade_pct), f"+{gain:.0f}m", f"-{abs(loss):.0f}m"))
        prev_dist, prev_time, prev_ele = dist_m, t, ele

    # Remainder after the last split (if any)
    remainder = total_dist - prev_dist
    if remainder > 1.0:
        split     = total_time - prev_time
        pace      = split / (remainder / 1000)
        grade_pct = (cum_ele[-1] - prev_ele) / remainder * 100
        gain, loss = elevation_stats(cum_dist, cum_ele, prev_dist, total_dist)
        total_gain += gain
        total_loss += loss
        label = f"+{remainder / 1000:.2f}*"
        print(row(label, fmt_time(split), fmt_pace(pace), fmt_time(total_time),
                  fmt_grade(grade_pct), f"+{gain:.0f}m", f"-{abs(loss):.0f}m"))

    print(divider)
    avg_pace = total_time / (total_dist / 1000)
    net_total = cum_ele[-1] - cum_ele[0]
    total_grade_pct = net_total / total_dist * 100
    print(row("TOTAL", fmt_time(total_time), fmt_pace(avg_pace), "",
              fmt_grade(total_grade_pct), f"+{total_gain:.0f}m", f"-{abs(total_loss):.0f}m"))

    print()
    if remainder > 1.0:
        print(f"  * Partial segment ({remainder:.0f} m)")
    print(f"  Avg actual pace  = {fmt_pace(avg_pace)}")
    print(f"  Target GAP       = {gap_label}")
    print(f"  (Difference reflects net elevation change over the route)")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Estimate per-km splits from a GPX file using the Strava GAP algorithm.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("gpx_file", help="Path to GPX file")
    ap.add_argument(
        "--gap", type=float, default=6.0, metavar="MIN/KM",
        help="Target Grade Adjusted Pace in min/km (default: 6.0)",
    )
    ap.add_argument(
        "--smooth", type=float, default=50, metavar="M",
        help="Gaussian smoothing sigma in metres (default: 50, increase for noisy GPS)",
    )
    ap.add_argument(
        "--splits", type=float, nargs="+", default=[1.0], metavar="KM",
        help=(
            "Split distances in km. A single value repeats every N km "
            "(e.g. --splits 5). Multiple values set explicit cumulative distances "
            "(e.g. --splits 5 10 21.1). Default: 1"
        ),
    )
    ap.add_argument(
        "--end-gap", type=float, default=None, metavar="MIN/KM",
        help=(
            "End GAP for a linear pace progression (min/km). "
            "If set, GAP increases linearly from --gap at the start "
            "to --end-gap at the finish. E.g. --gap 6.5 --end-gap 5.5"
        ),
    )
    ap.add_argument(
        "--write-virtual", action="store_true",
        help=(
            "Write a virtual training partner GPX file. "
            "Each track point gets a timestamp matching the GAP-derived pace plan. "
            "Output: <input>_virtual.gpx (override with --virtual-output)."
        ),
    )
    ap.add_argument(
        "--virtual-output", default=None, metavar="PATH",
        help="Output path for the virtual partner GPX (implies --write-virtual).",
    )
    args = ap.parse_args()

    points = parse_gpx(args.gpx_file)
    cum_dist, cum_time, cum_ele, *_ = build_profile(
        points, args.gap, args.smooth, end_gap_min_km=args.end_gap
    )
    split_points = make_split_points(args.splits, cum_dist[-1])
    print_results(args, cum_dist, cum_time, cum_ele, split_points, end_gap=args.end_gap)

    if args.write_virtual or args.virtual_output:
        out = args.virtual_output or (
            os.path.splitext(args.gpx_file)[0] + "_virtual.gpx"
        )
        pts_full = parse_gpx_with_times(args.gpx_file)
        elapsed  = compute_point_times(pts_full, args.gap, args.smooth,
                                       end_gap_min_km=args.end_gap)
        write_virtual_gpx(pts_full, elapsed, out, args.gap, args.end_gap)
        print(f"  Virtual partner GPX written → {out}")


if __name__ == "__main__":
    main()
