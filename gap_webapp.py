#!/usr/bin/env python3
"""
GAP Calculator – web interface

Run:  python gap_webapp.py
Open: http://localhost:8080

Requires: pip install flask
All GAP calculation logic lives in gap_calculator.py (no other dependencies).
"""

import io
import json
import math
import os
import sys
import tempfile
import uuid

from flask import Flask, request, render_template_string, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gap_calculator import (
    parse_gpx, parse_gpx_with_times, build_profile, interp_time, interp_ele,
    elevation_stats, make_split_points, fmt_time, fmt_pace, fmt_grade,
    compute_point_times, write_virtual_gpx, parse_recorded_times,
    smooth_elevation, gap_speed_factor,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload cap

# In-memory store: token → (gpx_content_str, download_filename)
_virtual_files: dict = {}
# Persistent uploaded file store: token → (bytes, filename)
_uploaded_files: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grade_badge(g):
    if g > 8:  return "bg-danger"
    if g > 4:  return "bg-warning"
    if g > 1:  return "bg-warning text-dark"
    if g > -2: return "bg-success"
    if g > -6: return "bg-info text-dark"
    return "bg-primary"


def _pace_color(pace_s, target_s):
    """Hex colour for a split's pace relative to its target GAP."""
    r = pace_s / target_s if target_s > 0 else 1.0
    if r < 0.90: return "#1d4ed8"   # blue   – much faster
    if r < 1.00: return "#22c55e"   # green  – on target / faster
    if r < 1.15: return "#eab308"   # yellow – slightly slower
    if r < 1.35: return "#f97316"   # orange – slower
    return "#dc2626"                 # red    – much slower


def _fmt_diff(diff_s):
    """Format a pace difference (s/km) as e.g. +0:15 or -1:03."""
    sign = "+" if diff_s >= 0 else "-"
    s = abs(int(round(diff_s)))
    m, sec = divmod(s, 60)
    return f"{sign}{m}:{sec:02d}"


def _downsample(arrays, n=400):
    """Return every k-th element from parallel arrays so len ≈ n."""
    length = len(arrays[0])
    if length <= n:
        return arrays
    step = max(1, length // n)
    return [a[::step] for a in arrays]


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

def run_calculation(gpx_bytes, filename, start_gap, end_gap, smooth, splits):
    with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False) as tmp:
        tmp.write(gpx_bytes)
        tmp_path = tmp.name
    try:
        points   = parse_gpx(tmp_path)
        pts_full = parse_gpx_with_times(tmp_path)
    finally:
        os.unlink(tmp_path)

    cum_dist, cum_time, cum_ele, cum_lats, cum_lons, cum_pace = build_profile(
        points, start_gap, smooth, end_gap_min_km=end_gap
    )
    split_points = make_split_points(splits, cum_dist[-1])

    # Recorded pace from GPX timestamps (None when file has no times)
    cum_rec = parse_recorded_times(pts_full)

    total_dist = cum_dist[-1]
    total_time = cum_time[-1]

    # ── Split rows ──────────────────────────────────────────────────────────
    rows = []
    prev_dist = prev_time = 0.0
    prev_ele  = cum_ele[0]
    total_gain = total_loss = 0.0

    for label, dist_m in split_points:
        dist_m    = min(dist_m, total_dist)
        t         = interp_time(cum_dist, cum_time, dist_m)
        ele       = interp_ele(cum_dist, cum_ele, dist_m)
        split     = t - prev_time
        seg_d     = dist_m - prev_dist
        pace_s    = split / (seg_d / 1000) if seg_d > 0 else 0.0
        grade_pct = (ele - prev_ele) / seg_d * 100 if seg_d > 0 else 0.0
        gain, loss = elevation_stats(cum_dist, cum_ele, prev_dist, dist_m)
        total_gain += gain
        total_loss += loss

        # Local target GAP at midpoint of this split
        if end_gap is not None:
            f = (prev_dist + seg_d / 2) / total_dist if total_dist > 0 else 0
            local_target_s = (start_gap + f * (end_gap - start_gap)) * 60
        else:
            local_target_s = start_gap * 60

        # Recorded pace and recorded GAP for this split
        if cum_rec is not None:
            rec_t0    = interp_time(cum_dist, cum_rec, prev_dist)
            rec_t1    = interp_time(cum_dist, cum_rec, dist_m)
            rec_split = rec_t1 - rec_t0
            rec_pace_s = rec_split / (seg_d / 1000) if seg_d > 0 else 0.0
            factor     = gap_speed_factor(grade_pct / 100)
            rec_gap_s  = rec_pace_s / factor if factor > 0 else rec_pace_s
            diff_s     = rec_gap_s - local_target_s   # GAP vs GAP target
        else:
            rec_pace_s = rec_gap_s = diff_s = None

        rows.append({
            "label":          label,
            "split":          fmt_time(split),
            "pace":           fmt_pace(pace_s),
            "pace_s":         round(pace_s, 1),
            "elapsed":        fmt_time(t),
            "grade":          fmt_grade(grade_pct),
            "grade_pct":      round(grade_pct, 2),
            "badge":          _grade_badge(grade_pct),
            "gain":           f"+{gain:.0f}",
            "loss":           f"-{abs(loss):.0f}",
            "is_partial":     False,
            "color":          _pace_color(pace_s, local_target_s),
            "local_target_s": round(local_target_s, 1),
            "rec_pace":       fmt_pace(rec_pace_s) if rec_pace_s is not None else "—",
            "rec_pace_s":     round(rec_pace_s, 1) if rec_pace_s is not None else None,
            "rec_gap":        fmt_pace(rec_gap_s)  if rec_gap_s  is not None else "—",
            "rec_gap_s":      round(rec_gap_s, 1)  if rec_gap_s  is not None else None,
            "diff_s":         round(diff_s, 1)     if diff_s     is not None else None,
            "diff_fmt":       _fmt_diff(diff_s)    if diff_s     is not None else "—",
        })
        prev_dist, prev_time, prev_ele = dist_m, t, ele

    remainder = total_dist - prev_dist
    if remainder > 1.0:
        split     = total_time - prev_time
        pace_s    = split / (remainder / 1000)
        grade_pct = (cum_ele[-1] - prev_ele) / remainder * 100
        gain, loss = elevation_stats(cum_dist, cum_ele, prev_dist, total_dist)
        total_gain += gain
        total_loss += loss
        if end_gap is not None:
            local_target_s = (start_gap + (end_gap - start_gap)) * 60
        else:
            local_target_s = start_gap * 60
        if cum_rec is not None:
            rec_t0    = interp_time(cum_dist, cum_rec, prev_dist)
            rec_split = cum_rec[-1] - rec_t0
            rec_pace_s = rec_split / (remainder / 1000) if remainder > 0 else 0.0
            factor     = gap_speed_factor(grade_pct / 100)
            rec_gap_s  = rec_pace_s / factor if factor > 0 else rec_pace_s
            diff_s     = rec_gap_s - local_target_s
        else:
            rec_pace_s = rec_gap_s = diff_s = None
        rows.append({
            "label":          f"+{remainder/1000:.2f}*",
            "split":          fmt_time(split),
            "pace":           fmt_pace(pace_s),
            "pace_s":         round(pace_s, 1),
            "elapsed":        fmt_time(total_time),
            "grade":          fmt_grade(grade_pct),
            "grade_pct":      round(grade_pct, 2),
            "badge":          _grade_badge(grade_pct),
            "gain":           f"+{gain:.0f}",
            "loss":           f"-{abs(loss):.0f}",
            "is_partial":     True,
            "color":          _pace_color(pace_s, local_target_s),
            "local_target_s": round(local_target_s, 1),
            "rec_pace":       fmt_pace(rec_pace_s) if rec_pace_s is not None else "—",
            "rec_pace_s":     round(rec_pace_s, 1) if rec_pace_s is not None else None,
            "rec_gap":        fmt_pace(rec_gap_s)  if rec_gap_s  is not None else "—",
            "rec_gap_s":      round(rec_gap_s, 1)  if rec_gap_s  is not None else None,
            "diff_s":         round(diff_s, 1)     if diff_s     is not None else None,
            "diff_fmt":       _fmt_diff(diff_s)    if diff_s     is not None else "—",
        })

    avg_pace_s = total_time / (total_dist / 1000)
    net_grade  = (cum_ele[-1] - cum_ele[0]) / total_dist * 100

    # ── Elevation + GAP profile (downsampled) ───────────────────────────────
    # cum_pace is in min/km; convert to s/km for chart
    cum_pace_s = [p * 60 for p in cum_pace]

    # Per-point recorded pace (s/km), smoothed to reduce GPS jitter
    if cum_rec is not None:
        raw_rec_pace = []
        for i in range(len(cum_dist)):
            if i == 0:
                raw_rec_pace.append(cum_rec[1] / (cum_dist[1] / 1000)
                                    if len(cum_dist) > 1 and cum_dist[1] > 0 else 0)
            else:
                dt = cum_rec[i] - cum_rec[i - 1]
                dd = cum_dist[i] - cum_dist[i - 1]
                raw_rec_pace.append(dt / dd * 1000 if dd > 0 and dt > 0 else raw_rec_pace[-1])
        smooth_rec_pace = smooth_elevation(raw_rec_pace, sigma=30)  # index-based, no distances
    else:
        smooth_rec_pace = None

    if smooth_rec_pace is not None:
        ds_dist, ds_ele, ds_pace, ds_rec = _downsample(
            [cum_dist, cum_ele, cum_pace_s, smooth_rec_pace], n=400
        )
        profile_rec_pace_s = [round(p, 1) for p in ds_rec]
    else:
        ds_dist, ds_ele, ds_pace = _downsample([cum_dist, cum_ele, cum_pace_s], n=400)
        profile_rec_pace_s = None

    profile_dist_km = [round(d / 1000, 3) for d in ds_dist]
    profile_ele     = [round(e, 1)        for e in ds_ele]
    profile_pace_s  = [round(p, 1)        for p in ds_pace]

    # Target GAP line (constant or linearly varying)
    if end_gap is not None:
        profile_target_s = [
            round((start_gap + (d / (total_dist / 1000)) * (end_gap - start_gap)) * 60, 1)
            for d in profile_dist_km
        ]
    else:
        profile_target_s = [start_gap * 60] * len(profile_dist_km)

    # ── Map data ─────────────────────────────────────────────────────────────
    # Full track (downsampled for transfer size)
    ds_lats, ds_lons = _downsample([cum_lats, cum_lons], n=800)
    track_coords = [[round(la, 6), round(lo, 6)]
                    for la, lo in zip(ds_lats, ds_lons)]

    # Per-split segments: lat/lon points within each distance band
    boundaries = [0.0] + [min(r["pace_s"] and dist_m, total_dist)   # placeholder
                          for r in rows]   # rebuild properly below
    # Proper boundary distances
    bound_dists = [0.0]
    for label, dist_m in split_points:
        bound_dists.append(min(dist_m, total_dist))
    if remainder > 1.0:
        bound_dists.append(total_dist)

    map_segments = []
    for i, row in enumerate(rows):
        start_d = bound_dists[i]
        end_d   = bound_dists[i + 1]
        coords  = [
            [round(la, 6), round(lo, 6)]
            for d, la, lo in zip(cum_dist, cum_lats, cum_lons)
            if start_d <= d <= end_d
        ]
        if len(coords) < 2:
            continue
        map_segments.append({
            "coords": coords,
            "color":  row["color"],
            "label":  row["label"],
            "pace":   row["pace"],
            "grade":  row["grade"],
            "gain":   row["gain"],
            "loss":   row["loss"],
        })

    center_lat = sum(cum_lats) / len(cum_lats)
    center_lon = sum(cum_lons) / len(cum_lons)

    # ── GAP label for header ─────────────────────────────────────────────────
    if end_gap:
        gap_label = f"{fmt_pace(start_gap * 60)} → {fmt_pace(end_gap * 60)}"
    else:
        gap_label = fmt_pace(start_gap * 60)

    # ── Virtual partner GPX ──────────────────────────────────────────────────
    elapsed  = compute_point_times(pts_full, start_gap, smooth, end_gap_min_km=end_gap)
    virt_gpx = write_virtual_gpx(pts_full, elapsed,
                                  start_gap_min_km=start_gap, end_gap_min_km=end_gap)
    dl_token  = str(uuid.uuid4())
    base      = os.path.splitext(filename)[0]
    virt_name = f"{base}_virtual.gpx"
    _virtual_files[dl_token] = (virt_gpx, virt_name)

    return {
        "filename":       filename,
        "dist_km":        f"{total_dist/1000:.2f}",
        "total_time":     fmt_time(total_time),
        "total_gain":     f"+{total_gain:.0f} m",
        "total_loss":     f"-{abs(total_loss):.0f} m",
        "avg_pace":       fmt_pace(avg_pace_s),
        "avg_pace_s":     round(avg_pace_s, 1),
        "gap_label":      gap_label,
        "target_gap_s":   start_gap * 60,
        "net_grade":      fmt_grade(net_grade),
        "net_badge":      _grade_badge(net_grade),
        "rows":           rows,
        "has_partial":    remainder > 1.0,
        "partial_m":      f"{remainder:.0f}",
        # Chart.js split pace chart
        "chart_labels":   json.dumps([r["label"]     for r in rows]),
        "chart_paces":    json.dumps([r["pace_s"]    for r in rows]),
        "chart_grades":   json.dumps([r["grade_pct"] for r in rows]),
        "chart_targets":  json.dumps([r["local_target_s"] for r in rows]),
        # Elevation+GAP profile chart
        "profile_dist":   json.dumps(profile_dist_km),
        "profile_ele":    json.dumps(profile_ele),
        "profile_pace":   json.dumps(profile_pace_s),
        "profile_target": json.dumps(profile_target_s),
        # Leaflet map
        "map_center":     json.dumps([round(center_lat, 5), round(center_lon, 5)]),
        "map_track":      json.dumps(track_coords),
        "map_segments":   json.dumps(map_segments),
        # Recorded pace + recorded GAP
        "has_recorded":      cum_rec is not None,
        "profile_rec_pace":  json.dumps(profile_rec_pace_s) if profile_rec_pace_s else "null",
        "chart_recorded":    json.dumps([r["rec_pace_s"] for r in rows])
                             if cum_rec is not None else "null",
        "chart_rec_gap":     json.dumps([r["rec_gap_s"] for r in rows])
                             if cum_rec is not None else "null",
        # Virtual partner download
        "download_token":    dl_token,
        "download_filename": virt_name,
        "csv_filename":      os.path.splitext(filename)[0] + "_splits.csv",
    }


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BZHD GAP Calculator</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body { background:#f0f2f5; }
    .sidebar { position:sticky; top:1rem; }
    .card { border:none; box-shadow:0 1px 4px rgba(0,0,0,.1); border-radius:.6rem; }
    .stat-card .value { font-size:1.4rem; font-weight:700; line-height:1.2; }
    .stat-card .label { font-size:.72rem; text-transform:uppercase;
                        letter-spacing:.05em; color:#6c757d; }
    .table th { font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }
    .table td { vertical-align:middle; }
    #map { height:380px; border-radius:.6rem; }
    .navbar-brand span { opacity:.65; font-weight:400; font-size:.9rem; }
    .legend-dot { display:inline-block; width:12px; height:12px;
                  border-radius:50%; margin-right:4px; }
    /* Sidebar collapse on mobile */
    #sidebarCollapse { display:none; }
    @media (max-width: 767px) {
      #sidebarCollapse { display:block; }
      #sidebarPanel { display:none; }
      #sidebarPanel.show { display:block; }
      .sidebar { position:static; }
    }
  </style>
</head>
<body>

<nav class="navbar mb-4" style="background:#1a1a2e">
  <div class="container-fluid px-4">
    <div class="navbar-brand text-white fw-bold">
      BZHD GAP Calculator <span class="ms-2">Grade Adjusted Pace</span>
    </div>
    <button id="sidebarCollapse" class="btn btn-outline-light btn-sm"
            onclick="document.getElementById('sidebarPanel').classList.toggle('show')">
      &#9776; Settings
    </button>
  </div>
</nav>

<div class="container-fluid px-4 pb-5">
  <div class="row g-4 align-items-start">

    <!-- ── Sidebar ── -->
    <div class="col-lg-3 col-md-4 sidebar">
      <div class="card">
        <div class="card-header fw-semibold">Settings</div>
        <div id="sidebarPanel" class="card-body">
          <form method="post" enctype="multipart/form-data">

            <div class="mb-3">
              <label class="form-label fw-semibold">GPX file</label>
              {% if form.file_token %}
              <div class="mb-1 text-muted" style="font-size:.82rem">
                <span class="badge bg-success me-1">&#10003;</span>{{ form.filename }}
              </div>
              <input type="file" name="gpx_file" class="form-control form-control-sm" accept=".gpx">
              <input type="hidden" name="file_token" value="{{ form.file_token }}">
              <div class="form-text">Leave blank to reuse the current file.</div>
              {% else %}
              <input type="file" name="gpx_file" class="form-control" accept=".gpx" required>
              {% endif %}
            </div>

            <hr class="my-3">
            <p class="text-muted mb-2" style="font-size:.8rem">PACE STRATEGY</p>

            <div class="mb-2">
              <label class="form-label fw-semibold">Start GAP
                <span class="text-muted fw-normal">(min/km)</span>
              </label>
              <input type="number" name="start_gap" class="form-control"
                     value="{{ form.start_gap }}" step="0.05" min="2" max="20">
            </div>

            <div class="mb-3">
              <label class="form-label fw-semibold">End GAP
                <span class="text-muted fw-normal">(min/km, optional)</span>
              </label>
              <input type="number" name="end_gap" class="form-control"
                     value="{{ form.end_gap }}" step="0.05" min="2" max="20"
                     placeholder="same as start = constant">
              <div class="form-text">Set for a linear negative-split plan.</div>
            </div>

            <hr class="my-3">
            <p class="text-muted mb-2" style="font-size:.8rem">SPLITS</p>

            <div class="mb-3">
              <label class="form-label fw-semibold">Splits
                <span class="text-muted fw-normal">(km)</span>
              </label>
              <input type="text" name="splits" class="form-control"
                     value="{{ form.splits }}" placeholder="e.g. 1  or  5 10 21.1">
              <div class="form-text">Single = repeat every N km.<br>
                Multiple = explicit distances.</div>
            </div>

            <div class="mb-4">
              <label class="form-label fw-semibold">Elevation smoothing
                <span class="text-muted fw-normal">(metres σ)</span>
              </label>
              <input type="number" name="smooth" class="form-control"
                     value="{{ form.smooth }}" min="1" max="500">
            </div>

            <button type="submit" class="btn btn-primary w-100 fw-semibold">
              Calculate
            </button>
          </form>

          {% if results %}
          <div class="mt-3 p-3 rounded" style="background:linear-gradient(135deg,#14532d,#166534);color:#fff;">
            <div class="fw-semibold mb-1" style="font-size:.9rem">Virtual Training Partner</div>
            <div style="font-size:.78rem;opacity:.85;" class="mb-2">
              GPX with GAP-derived timestamps for your device.
            </div>
            <a href="/download/{{ results.download_token }}"
               class="btn btn-light btn-sm fw-semibold text-success w-100">
              &#8659; Download {{ results.download_filename }}
            </a>
          </div>
          {% endif %}
        </div>
      </div>
    </div><!-- /sidebar -->

    <!-- ── Main ── -->
    <div class="col-lg-9 col-md-8">

      {% if error %}
      <div class="alert alert-danger">{{ error }}</div>
      {% endif %}

      {% if results %}

      <p class="text-muted mb-3">
        <strong>{{ results.filename }}</strong>
        &nbsp;·&nbsp; Target GAP: <strong>{{ results.gap_label }}</strong>
      </p>

      <!-- Summary cards -->
      <div class="row g-3 mb-4">
        <div class="col-6 col-sm-3">
          <div class="card stat-card text-center py-3">
            <div class="label">Distance</div>
            <div class="value">{{ results.dist_km }}<small class="fw-normal fs-6"> km</small></div>
          </div>
        </div>
        <div class="col-6 col-sm-3">
          <div class="card stat-card text-center py-3">
            <div class="label">Total Time</div>
            <div class="value">{{ results.total_time }}</div>
          </div>
        </div>
        <div class="col-6 col-sm-3">
          <div class="card stat-card text-center py-3">
            <div class="label">Elevation</div>
            <div class="value text-success">{{ results.total_gain }}</div>
            <div class="text-danger" style="font-size:.95rem">{{ results.total_loss }}</div>
          </div>
        </div>
        <div class="col-6 col-sm-3">
          <div class="card stat-card text-center py-3">
            <div class="label">Avg Pace</div>
            <div class="value">{{ results.avg_pace }}</div>
            <div class="text-muted" style="font-size:.8rem">GAP {{ results.gap_label }}</div>
          </div>
        </div>
      </div>

      <!-- Elevation + GAP profile -->
      <div class="card mb-4">
        <div class="card-header fw-semibold">Elevation &amp; Pace Profile
          <span class="ms-2 fw-normal text-muted" style="font-size:.82rem">
            actual running pace derived from terrain + target GAP
          </span>
        </div>
        <div class="card-body">
          <canvas id="profileChart" height="110"></canvas>
        </div>
      </div>

      <!-- Recorded vs Target Pace comparison chart -->
      {% if results.has_recorded %}
      <div class="card mb-4">
        <div class="card-header fw-semibold">Recorded vs Target Pace
          <span class="ms-2 fw-normal text-muted" style="font-size:.82rem">
            per-split recorded pace compared to target GAP
          </span>
        </div>
        <div class="card-body">
          <canvas id="compChart" height="100"></canvas>
        </div>
      </div>
      {% endif %}

      <!-- Map -->
      <div class="card mb-4">
        <div class="card-header fw-semibold">Route Map
          <span class="ms-2 fw-normal text-muted" style="font-size:.82rem">
            segments coloured by pace vs target GAP &nbsp;
            <span class="legend-dot" style="background:#1d4ed8"></span>fast
            <span class="legend-dot ms-1" style="background:#22c55e"></span>on target
            <span class="legend-dot ms-1" style="background:#eab308"></span>–15%
            <span class="legend-dot ms-1" style="background:#f97316"></span>–35%
            <span class="legend-dot ms-1" style="background:#dc2626"></span>slow
          </span>
        </div>
        <div class="card-body p-2">
          <div id="map"></div>
        </div>
      </div>

      <!-- Table -->
      <div class="card">
        <div class="card-header fw-semibold d-flex align-items-center justify-content-between">
          Split Details
          <button onclick="exportCSV()" class="btn btn-outline-secondary btn-sm">
            &#8659; Export CSV
          </button>
        </div>
        <div class="table-responsive">
          <table id="splitsTable" class="table table-hover mb-0">
            <thead class="table-dark">
              <tr>
                <th>km</th><th>Split</th><th>Pace</th><th>Elapsed</th>
                <th>Avg Grade</th><th>Gain (m)</th><th>Loss (m)</th>
                {% if results.has_recorded %}
                <th>Rec Pace</th><th>Rec GAP</th><th>vs Target</th>
                {% endif %}
              </tr>
            </thead>
            <tbody>
              {% for row in results.rows %}
              <tr class="{{ 'table-secondary' if row.is_partial else '' }}">
                <td class="fw-semibold">
                  <span class="me-1" style="display:inline-block;width:10px;height:10px;
                        border-radius:2px;background:{{ row.color }}"></span>
                  {{ row.label }}
                </td>
                <td>{{ row.split }}</td>
                <td>{{ row.pace }}</td>
                <td class="text-muted">{{ row.elapsed }}</td>
                <td><span class="badge {{ row.badge }}">{{ row.grade }}</span></td>
                <td class="text-success fw-semibold">{{ row.gain }}</td>
                <td class="text-danger fw-semibold">{{ row.loss }}</td>
                {% if results.has_recorded %}
                <td>{{ row.rec_pace }}</td>
                <td class="fw-semibold">{{ row.rec_gap }}</td>
                <td class="{{ 'text-danger' if row.diff_s and row.diff_s > 0 else ('text-success' if row.diff_s and row.diff_s < 0 else '') }} fw-semibold">{{ row.diff_fmt }}</td>
                {% endif %}
              </tr>
              {% endfor %}
            </tbody>
            <tfoot class="table-dark">
              <tr>
                <td class="fw-bold">TOTAL</td>
                <td class="fw-bold">{{ results.total_time }}</td>
                <td class="fw-bold">{{ results.avg_pace }}</td>
                <td></td>
                <td><span class="badge {{ results.net_badge }}">{{ results.net_grade }}</span></td>
                <td class="fw-bold text-success">{{ results.total_gain }}</td>
                <td class="fw-bold text-danger">{{ results.total_loss }}</td>
                {% if results.has_recorded %}
                <td></td><td></td><td></td>
                {% endif %}
              </tr>
            </tfoot>
          </table>
        </div>
        {% if results.has_partial %}
        <div class="card-footer text-muted" style="font-size:.85rem">
          * Partial segment ({{ results.partial_m }} m)
        </div>
        {% endif %}
      </div>

      {% else %}

      <div class="card text-center py-5">
        <div class="card-body">
          <p class="display-6 mb-2">⛰</p>
          <h5 class="mb-1">Upload a GPX file to get started</h5>
          <p class="text-muted mb-0">
            Estimates per-split pace from terrain using Strava's GAP algorithm.<br>
            Supports constant pace or a linear negative-split progression.
          </p>
        </div>
      </div>

      {% endif %}
    </div><!-- /main -->
  </div>
</div>

{% if results %}
<script>
(function () {
  /* ── shared formatter ── */
  function fmtPace(s) {
    const m = Math.floor(s / 60), sec = Math.round(s % 60);
    return `${m}:${sec.toString().padStart(2,'0')}`;
  }

  /* ── 1. Elevation + GAP profile chart ── */
  const profDist    = {{ results.profile_dist    | safe }};
  const profEle     = {{ results.profile_ele    | safe }};
  const profPace    = {{ results.profile_pace   | safe }};
  const profTarget  = {{ results.profile_target | safe }};
  const profRecPace = {{ results.profile_rec_pace | safe }};

  const eleMin = Math.min(...profEle);
  const eleMax = Math.max(...profEle);
  const elePad = (eleMax - eleMin) * 0.12;

  // Pace axis bounds — must be computed from actual data so the scale
  // tightens/widens correctly when the smoothing setting changes.
  const allProfPace = [...profPace, ...profTarget, ...(profRecPace || [])];
  const paceMin = Math.min(...allProfPace);
  const paceMax = Math.max(...allProfPace);
  const pacePad = (paceMax - paceMin) * 0.08;

  new Chart(document.getElementById('profileChart'), {
    type: 'line',
    data: {
      labels: profDist,
      datasets: [
        {
          label: 'Elevation (m)',
          data: profEle,
          fill: true,
          backgroundColor: 'rgba(148,163,184,0.25)',
          borderColor: 'rgba(100,116,139,0.7)',
          borderWidth: 1.5,
          pointRadius: 0,
          yAxisID: 'yEle',
          order: 3,
        },
        {
          label: 'Actual pace',
          data: profPace,
          fill: false,
          borderColor: 'rgba(239,68,68,0.85)',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'yPace',
          order: 2,
        },
        {
          label: 'Target GAP',
          data: profTarget,
          fill: false,
          borderColor: 'rgba(0,0,0,0.55)',
          borderWidth: 1.5,
          borderDash: [6, 4],
          pointRadius: 0,
          yAxisID: 'yPace',
          order: 1,
        },
        ...(profRecPace ? [{
          label: 'Recorded pace',
          data: profRecPace,
          fill: false,
          borderColor: 'rgba(59,130,246,0.85)',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'yPace',
          order: 0,
        }] : []),
      ],
    },
    options: {
      responsive: true,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            title: ctx => `${ctx[0].label} km`,
            label: ctx => {
              if (ctx.dataset.yAxisID === 'yEle')
                return `Elevation: ${ctx.raw} m`;
              return `${ctx.dataset.label}: ${fmtPace(ctx.raw)} /km`;
            },
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: 'Distance (km)' },
          ticks: { maxTicksLimit: 12, callback: (_, i) => profDist[i] },
        },
        yEle: {
          type: 'linear', position: 'left',
          min: eleMin - elePad, max: eleMax + elePad,
          title: { display: true, text: 'Elevation (m)' },
          ticks: { callback: v => `${v.toFixed(0)} m` },
        },
        yPace: {
          type: 'linear', position: 'right',
          reverse: true,
          min: paceMin - pacePad,
          max: paceMax + pacePad,
          title: { display: true, text: 'Pace (min/km)' },
          ticks: { callback: fmtPace },
          grid: { drawOnChartArea: false },
        },
      },
    },
  });

  /* ── 2. Recorded vs Target comparison chart ── */
  {% if results.has_recorded %}
  {
    const compLabels   = {{ results.chart_labels   | safe }};
    const compRecorded = {{ results.chart_recorded | safe }};
    const compRecGap   = {{ results.chart_rec_gap  | safe }};
    const compTargets  = {{ results.chart_targets  | safe }};

    const validRec = compRecorded.filter(v => v !== null);
    const validGap = compRecGap.filter(v => v !== null);
    const compAll  = [...validRec, ...validGap, ...compTargets];
    const compMin  = Math.max(0, Math.min(...compAll) * 0.92);
    const compMax  = Math.max(...compAll) * 1.08;

    new Chart(document.getElementById('compChart'), {
      type: 'bar',
      data: {
        labels: compLabels,
        datasets: [
          {
            label: 'Recorded pace',
            data: compRecorded,
            backgroundColor: compRecGap.map((gap, i) => {
              if (gap === null) return 'rgba(156,163,175,0.5)';
              const diff = gap - compTargets[i];
              if (diff < -15) return 'rgba(29,78,216,0.75)';
              if (diff <   0) return 'rgba(34,197,94,0.75)';
              if (diff <  15) return 'rgba(234,179,8,0.75)';
              if (diff <  30) return 'rgba(249,115,22,0.75)';
              return 'rgba(220,38,38,0.75)';
            }),
            borderRadius: 3,
            order: 3,
          },
          {
            label: 'Recorded GAP',
            type: 'line',
            data: compRecGap,
            borderColor: 'rgba(59,130,246,0.9)',
            borderWidth: 2.5,
            pointRadius: 3,
            pointBackgroundColor: 'rgba(59,130,246,0.9)',
            fill: false,
            order: 2,
          },
          {
            label: 'Target GAP',
            type: 'line',
            data: compTargets,
            borderColor: 'rgba(0,0,0,0.7)',
            borderWidth: 2,
            borderDash: [7, 4],
            pointRadius: 0,
            fill: false,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'bottom' },
          tooltip: {
            callbacks: {
              label: ctx => {
                if (ctx.raw === null || ctx.raw === undefined) return `${ctx.dataset.label}: —`;
                return `${ctx.dataset.label}: ${fmtPace(ctx.raw)} /km`;
              },
            },
          },
        },
        scales: {
          y: {
            min: compMin, max: compMax,
            reverse: true,
            ticks: { callback: fmtPace },
            title: { display: true, text: 'Pace (min/km)' },
          },
          x: { title: { display: true, text: 'Split (km)' } },
        },
      },
    });
  }
  {% endif %}

  /* ── 4. Leaflet map ── */
  const center   = {{ results.map_center   | safe }};
  const track    = {{ results.map_track    | safe }};
  const segments = {{ results.map_segments | safe }};

  const map = L.map('map').setView(center, 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);

  // Thin grey underlay for the full track
  const trackLine = L.polyline(track, { color: '#94a3b8', weight: 2, opacity: 0.5 });
  trackLine.addTo(map);

  // Coloured split segments
  segments.forEach(seg => {
    L.polyline(seg.coords, { color: seg.color, weight: 5, opacity: 0.9 })
      .addTo(map)
      .bindPopup(
        `<strong>${seg.label} km</strong><br>` +
        `Pace: ${seg.pace}<br>Grade: ${seg.grade}<br>` +
        `Gain: ${seg.gain} m &nbsp; Loss: ${seg.loss} m`
      );
  });

  map.fitBounds(trackLine.getBounds(), { padding: [16, 16] });

}());

function exportCSV() {
  const tbl = document.getElementById('splitsTable');
  const rows = [...tbl.querySelectorAll('tr')];
  const csv = rows.map(tr =>
    [...tr.querySelectorAll('th,td')]
      .map(cell => {
        // Strip inner HTML (badge spans, colour swatches) — text only
        const txt = cell.innerText.trim().replace(/"/g, '""');
        return `"${txt}"`;
      })
      .join(',')
  ).join('\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = {{ results.csv_filename | tojson }};
  a.click();
  URL.revokeObjectURL(a.href);
}
</script>
{% endif %}

</body>
</html>"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    form = {
        "start_gap": "6.0", "end_gap": "", "splits": "1", "smooth": "50",
        "file_token": "", "filename": "",
    }
    results = None
    error   = None

    if request.method == "POST":
        form["start_gap"]  = request.form.get("start_gap",  "6.0").strip()
        form["end_gap"]    = request.form.get("end_gap",    "").strip()
        form["splits"]     = request.form.get("splits",     "1").strip()
        form["smooth"]     = request.form.get("smooth",     "50").strip()
        form["file_token"] = request.form.get("file_token", "").strip()

        file = request.files.get("gpx_file")

        # Resolve file: new upload takes priority, then stored token
        if file and file.filename:
            gpx_bytes = file.read()
            fname     = file.filename
            new_token = str(uuid.uuid4())
            _uploaded_files[new_token] = (gpx_bytes, fname)
            form["file_token"] = new_token
            form["filename"]   = fname
        elif form["file_token"] in _uploaded_files:
            gpx_bytes, fname = _uploaded_files[form["file_token"]]
            form["filename"]  = fname
        else:
            error = "Please select a GPX file."
            gpx_bytes = None

        if gpx_bytes:
            try:
                start_gap = float(form["start_gap"])
                end_gap   = float(form["end_gap"]) if form["end_gap"] else None
                smooth    = float(form["smooth"])
                splits    = [float(x) for x in form["splits"].split()]
                if not splits:
                    raise ValueError("Empty splits value.")
                results = run_calculation(gpx_bytes, fname, start_gap, end_gap, smooth, splits)
            except Exception as exc:
                error = str(exc)

    return render_template_string(HTML, form=form, results=results, error=error)


@app.route("/download/<token>")
def download(token):
    if token not in _virtual_files:
        return "File not found or already downloaded.", 404
    content, filename = _virtual_files.pop(token)
    return send_file(
        io.BytesIO(content.encode("utf-8")),
        mimetype="application/gpx+xml",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
