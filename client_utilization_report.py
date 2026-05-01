#!/usr/bin/env python3
"""
Tiderise Weekly Client Utilization Report Generator (Remote Routines version)
 
Pulls data live from the Clockify API (no XLSX export needed) and produces
the same dark-themed HTML dashboard as the original local version.
 
Required environment variables:
  CLOCKIFY_API_KEY        - Personal API key from Clockify > Profile > Advanced
  CLOCKIFY_WORKSPACE_ID   - Workspace ID from the Clockify URL
 
Usage:
  python3 client_utilization_report.py --week previous --output-dir ./reports
 
Output:
  - HTML dashboard saved to <output-dir>/Weekly_Client_Utilization_<dates>.html
  - Human-readable log to stderr
  - Machine-readable JSON summary to stdout, prefixed by '===SUMMARY===' marker
"""
 
import argparse
import json
import os
import sys
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
 
# ── Configuration ────────────────────────────────────────────────────────────
INTERNAL_CLIENT = "Tiderise MW"
API_BASE = "https://api.clockify.me/api/v1"
REPORTS_BASE = "https://reports.api.clockify.me/v1"
 
CLOCKIFY_API_KEY = os.environ.get("CLOCKIFY_API_KEY")
CLOCKIFY_WORKSPACE_ID = os.environ.get("CLOCKIFY_WORKSPACE_ID")
 
if not CLOCKIFY_API_KEY or not CLOCKIFY_WORKSPACE_ID:
    print("ERROR: CLOCKIFY_API_KEY and CLOCKIFY_WORKSPACE_ID env vars required", file=sys.stderr)
    sys.exit(1)
 
HEADERS = {"X-Api-Key": CLOCKIFY_API_KEY, "Content-Type": "application/json"}
 
 
# ── Date range calculation ───────────────────────────────────────────────────
def get_reporting_week(week_arg="previous"):
    """Return (start_date, end_date) as date objects for the requested week.
 
    'current'  -> current Mon-Sun (week containing today)
    'previous' -> previous Mon-Sun (the week that just ended)  [default]
    """
    today = datetime.now(timezone.utc).date()
    monday_offset = today.weekday()  # Mon=0 ... Sun=6
    if week_arg == "current":
        start = today - timedelta(days=monday_offset)
    else:
        start = today - timedelta(days=monday_offset + 7)
    end = start + timedelta(days=6)
    return start, end
 
 
# ── Fetch project → client mapping (Projects API) ────────────────────────────
def fetch_project_clients():
    """Return {projectId: {'project_name': str, 'client_name': str}} for ALL projects.
 
    This is the authoritative source for the client a project belongs to —
    used to ensure internal Tiderise MW projects are correctly attributed
    even when they have no scheduled assignment.
    """
    url = f"{API_BASE}/workspaces/{CLOCKIFY_WORKSPACE_ID}/projects"
    params = {"page-size": 5000, "archived": "false"}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
    resp.raise_for_status()
    projects = resp.json()
    return {
        p["id"]: {
            "project_name": p.get("name", "Unknown"),
            "client_name": p.get("clientName") or "(No Client)",
        }
        for p in projects
    }
 
 
# ── Fetch tracked hours (Reports API) ────────────────────────────────────────
def fetch_tracked_hours(start_date, end_date):
    """Return {projectId: {'hours': float, 'project_name': str}}."""
    body = {
        "dateRangeStart": f"{start_date.isoformat()}T00:00:00.000Z",
        "dateRangeEnd": f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00.000Z",
        "summaryFilter": {"groups": ["PROJECT"]},
        "exportType": "JSON",
    }
    url = f"{REPORTS_BASE}/workspaces/{CLOCKIFY_WORKSPACE_ID}/reports/summary"
    resp = requests.post(url, headers=HEADERS, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
 
    tracked = {}
    for grp in data.get("groupOne", []):
        pid = grp.get("_id") or grp.get("id")
        if not pid:
            continue
        tracked[pid] = {
            "hours": grp.get("duration", 0) / 3600.0,
            "project_name": grp.get("name", "Unknown"),
        }
    return tracked
 
 
# ── Fetch scheduled hours (Scheduling API) ───────────────────────────────────
def fetch_scheduled_hours(start_date, end_date):
    """Return {projectId: {'hours': float, 'project_name': str, 'client_name': str}}.
 
    Scheduled hours = hoursPerDay * number of weekdays (Mon-Fri) in the reporting
    week that fall within the assignment's period.
    """
    url = f"{API_BASE}/workspaces/{CLOCKIFY_WORKSPACE_ID}/scheduling/assignments/all"
    page_size = 200
    page = 1
    assignments = []
    while True:
        params = {
            "start": f"{start_date.isoformat()}T00:00:00Z",
            "end": f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
            "page": page,
            "page-size": page_size,
        }
        resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        assignments.extend(batch)
        if len(batch) < page_size:
            break  # last page
        page += 1
        if page > 50:  # safety guard against infinite loop
            print("WARNING: hit 50-page safety limit on scheduling assignments", file=sys.stderr)
            break
    print(f"Scheduling API returned {len(assignments)} total assignments across {page} page(s)", file=sys.stderr)
 
    scheduled = {}
    for a in assignments:
        pid = a.get("projectId")
        proj_name = a.get("projectName", "Unknown")
        client_name = a.get("clientName") or "(No Client)"
        hours_per_day = a.get("hoursPerDay", 0) or 0
        period = a.get("period") or {}
 
        try:
            period_start = datetime.fromisoformat(
                period["start"].replace("Z", "+00:00")
            ).date()
            period_end = datetime.fromisoformat(
                period["end"].replace("Z", "+00:00")
            ).date()
        except (KeyError, ValueError, TypeError):
            continue
 
        # Overlap with reporting week
        overlap_start = max(start_date, period_start)
        overlap_end = min(end_date, period_end)
        if overlap_start > overlap_end:
            continue
 
        # Weekdays only (Mon-Fri)
        weekday_count = sum(
            1
            for d in range((overlap_end - overlap_start).days + 1)
            if (overlap_start + timedelta(days=d)).weekday() < 5
        )
 
        sched_hours = weekday_count * hours_per_day
        if pid not in scheduled:
            scheduled[pid] = {
                "hours": 0.0,
                "project_name": proj_name,
                "client_name": client_name,
            }
        scheduled[pid]["hours"] += sched_hours
 
    return scheduled
 
 
# ── Build merged DataFrame matching original XLSX shape ──────────────────────
def fetch_assignments_dataframe(week_arg="previous"):
    """Return (df, start_dt, end_dt). DataFrame columns:
    Project, Scheduled, Tracked, ProjectName, Client.
    """
    start_date, end_date = get_reporting_week(week_arg)
    print(f"Reporting window: {start_date} to {end_date}", file=sys.stderr)
 
    project_meta = fetch_project_clients()
    tracked = fetch_tracked_hours(start_date, end_date)
    scheduled = fetch_scheduled_hours(start_date, end_date)
    print(
        f"Fetched {len(project_meta)} projects, {len(tracked)} tracked, "
        f"{len(scheduled)} scheduled",
        file=sys.stderr,
    )
 
    all_pids = set(tracked.keys()) | set(scheduled.keys())
    rows = []
    for pid in all_pids:
        s = scheduled.get(pid, {})
        t = tracked.get(pid, {})
        meta = project_meta.get(pid, {})
        # Authoritative client comes from Projects API; fall back to scheduling, then Unknown
        proj_name = (
            meta.get("project_name")
            or s.get("project_name")
            or t.get("project_name", "Unknown")
        )
        client_name = (
            meta.get("client_name")
            or s.get("client_name")
            or "(Unknown Client)"
        )
        rows.append({
            "ProjectName": proj_name,
            "Client": client_name,
            "Scheduled": s.get("hours", 0.0),
            "Tracked": t.get("hours", 0.0),
            "Project": f"{proj_name} - {client_name}",
        })
 
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ProjectName", "Client", "Scheduled", "Tracked", "Project"]
    )
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.min.time())
    return df, start_dt, end_dt
 
 
# ── Build report aggregations ────────────────────────────────────────────────
def build_report(df):
    client_df = df[df['Client'] != INTERNAL_CLIENT].copy()
 
    client_summary = client_df.groupby('Client').agg(
        Scheduled=('Scheduled', 'sum'),
        Tracked=('Tracked', 'sum'),
    ).reset_index()
    client_summary['Utilization'] = client_summary.apply(
        lambda r: (r['Tracked'] / r['Scheduled'] * 100) if r['Scheduled'] > 0 else None,
        axis=1,
    )
    client_summary['Difference'] = client_summary['Tracked'] - client_summary['Scheduled']
    client_summary.sort_values('Tracked', ascending=False, inplace=True)
 
    project_detail = client_df.groupby(['Client', 'ProjectName']).agg(
        Scheduled=('Scheduled', 'sum'),
        Tracked=('Tracked', 'sum'),
    ).reset_index()
    project_detail['Utilization'] = project_detail.apply(
        lambda r: (r['Tracked'] / r['Scheduled'] * 100) if r['Scheduled'] > 0 else None,
        axis=1,
    )
 
    total_scheduled = client_summary['Scheduled'].sum()
    total_tracked = client_summary['Tracked'].sum()
    overall_util = (total_tracked / total_scheduled * 100) if total_scheduled > 0 else 0
    num_clients = len(client_summary)
 
    healthy = len(client_summary[client_summary['Utilization'] >= 80])
    attention = len(client_summary[(client_summary['Utilization'] >= 65) & (client_summary['Utilization'] < 80)])
    at_risk = len(client_summary[(client_summary['Utilization'].notna()) & (client_summary['Utilization'] < 65)])
    zero_tracked = len(client_summary[client_summary['Tracked'] == 0])
    unscheduled = len(client_summary[client_summary['Scheduled'] == 0])
 
    return {
        'client_summary': client_summary,
        'project_detail': project_detail,
        'total_scheduled': total_scheduled,
        'total_tracked': total_tracked,
        'overall_util': overall_util,
        'num_clients': num_clients,
        'healthy': healthy,
        'attention': attention,
        'at_risk': at_risk,
        'zero_tracked': zero_tracked,
        'unscheduled': unscheduled,
    }
 
 
# ── Status helpers ───────────────────────────────────────────────────────────
def get_status(util, scheduled, tracked):
    if scheduled == 0 and tracked > 0:
        return ('Unscheduled', '#6366f1')
    if tracked == 0 and scheduled > 0:
        return ('Zero Tracked', '#64748b')
    if util is None:
        return ('—', '#64748b')
    if util >= 80:
        return ('Healthy', '#10b981')
    if util >= 65:
        return ('Needs Attention', '#f59e0b')
    return ('At Risk', '#ef4444')
 
 
def status_badge(label, color):
    return f'<span style="background:{color}22;color:{color};padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap">{label}</span>'
 
 
def util_color(util):
    if util is None:
        return '#64748b'
    if util >= 80:
        return '#10b981'
    if util >= 65:
        return '#f59e0b'
    return '#ef4444'
 
 
# ── Gauge SVG ────────────────────────────────────────────────────────────────
def gauge_svg(value, label, max_val=100):
    import math
    pct = min(max(value / max_val, 0), 1)
    cx, cy, r = 100, 110, 70
 
    def point(angle):
        rad = math.radians(angle)
        return cx + r * math.cos(rad), cy - r * math.sin(rad)
 
    def arc_path(start_pct, end_pct, color):
        a1 = 180 - start_pct * 180
        a2 = 180 - end_pct * 180
        x1, y1 = point(a1)
        x2, y2 = point(a2)
        large = 1 if (a1 - a2) > 180 else 0
        return f'<path d="M {x1:.2f} {y1:.2f} A {r} {r} 0 {large} 1 {x2:.2f} {y2:.2f}" stroke="{color}" stroke-width="10" fill="none" stroke-linecap="round" opacity="0.3"/>'
 
    needle_angle = 180 - pct * 180
    nx = cx + (r - 10) * math.cos(math.radians(needle_angle))
    ny = cy - (r - 10) * math.sin(math.radians(needle_angle))
    needle_color = util_color(value)
 
    arcs = (
        arc_path(0, 0.65, '#ef4444')
        + arc_path(0.65, 0.80, '#f59e0b')
        + arc_path(0.80, 1.0, '#10b981')
    )
 
    return f'''<svg viewBox="0 0 200 140" style="width:180px;height:126px">
        {arcs}
        <line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{needle_color}" stroke-width="3" stroke-linecap="round"/>
        <circle cx="{cx}" cy="{cy}" r="6" fill="{needle_color}"/>
        <text x="{cx}" y="100" text-anchor="middle" fill="#e2e8f0" font-size="22" font-weight="700">{value:.1f}%</text>
    </svg>'''
 
 
# ── Generate HTML (unchanged from original) ──────────────────────────────────
def generate_html(data, start_date, end_date):
    cs = data['client_summary']
    pd_detail = data['project_detail']
 
    start_str = start_date.strftime('%b %d') if start_date else '?'
    end_str = end_date.strftime('%b %d, %Y') if end_date else '?'
    date_range = f"{start_str} – {end_str}"
    gen_time = datetime.now().strftime('%b %d, %Y %I:%M %p')
 
    kpi_cards = f'''
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
      <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Overall Utilization</div>
        <div style="color:#e2e8f0;font-size:32px;font-weight:700;line-height:1">{data["overall_util"]:.1f}%</div>
        <div style="color:#64748b;font-size:13px;margin-top:8px"><span style="color:{util_color(data["overall_util"])};font-weight:600">{get_status(data["overall_util"], 1, 1)[0]}</span></div>
      </div>
      <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Client Hours Tracked</div>
        <div style="color:#e2e8f0;font-size:32px;font-weight:700;line-height:1">{data["total_tracked"]:.0f}h</div>
        <div style="color:#64748b;font-size:13px;margin-top:8px">of {data["total_scheduled"]:.0f}h scheduled</div>
      </div>
      <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Active Clients</div>
        <div style="color:#e2e8f0;font-size:32px;font-weight:700;line-height:1">{data["num_clients"]}</div>
        <div style="color:#64748b;font-size:13px;margin-top:8px">{len(cs[cs['Tracked'] > 0])} with tracked hours</div>
      </div>
      <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Variance</div>
        <div style="color:#e2e8f0;font-size:32px;font-weight:700;line-height:1">{data["total_tracked"] - data["total_scheduled"]:+.0f}h</div>
        <div style="color:#64748b;font-size:13px;margin-top:8px">tracked vs. scheduled delta</div>
      </div>
    </div>'''
 
    gauges = f'''
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px">
      <div style="background:#1a1d27;border-radius:16px;padding:20px;text-align:center;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Overall Client Utilization</div>
        {gauge_svg(data["overall_util"], "Overall")}
      </div>
      <div style="background:#1a1d27;border-radius:16px;padding:20px;text-align:center;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Healthy Client Rate</div>
        {gauge_svg((data["healthy"] / max(data["num_clients"],1)) * 100, "Healthy Rate")}
      </div>
      <div style="background:#1a1d27;border-radius:16px;padding:20px;text-align:center;border:1px solid #2a2d3a">
        <div style="color:#94a3b8;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Schedule Coverage</div>
        {gauge_svg(min((data["total_tracked"] / max(data["total_scheduled"],1)) * 100, 100), "Coverage")}
      </div>
    </div>'''
 
    health_cards = f'''
    <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
      <div style="color:#e2e8f0;font-size:16px;font-weight:700;margin-bottom:16px;text-transform:uppercase;letter-spacing:0.5px">Client Health Summary</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
        <div style="background:#1a1d27;border-radius:12px;padding:18px;text-align:center;border:1px solid #2a2d3a;border-left:4px solid #10b981">
          <div style="color:#10b981;font-size:32px;font-weight:700">{data["healthy"]}</div>
          <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;font-weight:600">Healthy (&ge;80%)</div>
        </div>
        <div style="background:#1a1d27;border-radius:12px;padding:18px;text-align:center;border:1px solid #2a2d3a;border-left:4px solid #f59e0b">
          <div style="color:#f59e0b;font-size:32px;font-weight:700">{data["attention"]}</div>
          <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;font-weight:600">Needs Attention (65-79%)</div>
        </div>
        <div style="background:#1a1d27;border-radius:12px;padding:18px;text-align:center;border:1px solid #2a2d3a;border-left:4px solid #ef4444">
          <div style="color:#ef4444;font-size:32px;font-weight:700">{data["at_risk"]}</div>
          <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;font-weight:600">At Risk (&lt;65%)</div>
        </div>
        <div style="background:#1a1d27;border-radius:12px;padding:18px;text-align:center;border:1px solid #2a2d3a;border-left:4px solid #64748b">
          <div style="color:#64748b;font-size:32px;font-weight:700">{data["zero_tracked"]}</div>
          <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;font-weight:600">Zero Tracked</div>
        </div>
      </div>
    </div>'''
 
    client_rows = ''
    for _, row in cs.iterrows():
        client = row['Client']
        sched = row['Scheduled']
        tracked = row['Tracked']
        util = row['Utilization']
        status_label, status_color = get_status(util, sched, tracked)
        util_str = f"{util:.1f}%" if util is not None else "—"
        diff = tracked - sched
        diff_str = f"{diff:+.1f}h"
        diff_color = '#10b981' if diff >= 0 else '#ef4444'
 
        client_rows += f'''<tr style="border-bottom:1px solid #2a2d3a;background:#1a1d27">
          <td style="padding:14px;color:#e2e8f0;font-size:13px;font-weight:600">{client}</td>
          <td style="padding:14px;color:#94a3b8;font-size:13px;text-align:right">{sched:.1f}</td>
          <td style="padding:14px;color:#94a3b8;font-size:13px;text-align:right">{tracked:.1f}</td>
          <td style="padding:14px;color:{diff_color};font-size:13px;text-align:right;font-weight:500">{diff_str}</td>
          <td style="padding:14px;color:#e2e8f0;font-size:13px;text-align:right;font-weight:600">
            <div style="display:flex;align-items:center;justify-content:flex-end;gap:8px">
              <div style="width:60px;height:6px;background:#0f1117;border-radius:3px;overflow:hidden">
                <div style="height:100%;width:{min(util or 0, 100):.0f}%;background:{util_color(util)};border-radius:3px"></div>
              </div>
              {util_str}
            </div>
          </td>
          <td style="padding:14px;text-align:right">{status_badge(status_label, status_color)}</td>
        </tr>'''
 
        projects = pd_detail[pd_detail['Client'] == client].sort_values('Tracked', ascending=False)
        for _, proj in projects.iterrows():
            p_util = proj['Utilization']
            p_util_str = f"{p_util:.1f}%" if p_util is not None else "—"
            p_diff = proj['Tracked'] - proj['Scheduled']
            p_diff_str = f"{p_diff:+.1f}h"
            p_diff_color = '#10b981' if p_diff >= 0 else '#ef4444'
            p_status_label, p_status_color = get_status(p_util, proj['Scheduled'], proj['Tracked'])
 
            client_rows += f'''<tr style="border-bottom:1px solid #1e2130">
              <td style="padding:10px 14px 10px 36px;color:#94a3b8;font-size:12px">↳ {proj["ProjectName"]}</td>
              <td style="padding:10px 14px;color:#64748b;font-size:12px;text-align:right">{proj["Scheduled"]:.1f}</td>
              <td style="padding:10px 14px;color:#64748b;font-size:12px;text-align:right">{proj["Tracked"]:.1f}</td>
              <td style="padding:10px 14px;color:{p_diff_color};font-size:12px;text-align:right">{p_diff_str}</td>
              <td style="padding:10px 14px;color:#94a3b8;font-size:12px;text-align:right">
                <div style="display:flex;align-items:center;justify-content:flex-end;gap:8px">
                  <div style="width:40px;height:4px;background:#0f1117;border-radius:2px;overflow:hidden">
                    <div style="height:100%;width:{min(p_util or 0, 100):.0f}%;background:{util_color(p_util)};border-radius:2px"></div>
                  </div>
                  {p_util_str}
                </div>
              </td>
              <td style="padding:10px 14px;text-align:right"><span style="color:{p_status_color};font-size:10px;font-weight:600">{p_status_label}</span></td>
            </tr>'''
 
    top_clients = cs[cs['Tracked'] > 0].head(10)
    max_tracked = top_clients['Tracked'].max() if len(top_clients) > 0 else 1
    bar_rows = ''
    for _, row in top_clients.iterrows():
        pct = (row['Tracked'] / max_tracked) * 100
        bar_rows += f'''<div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="color:#e2e8f0;font-size:13px;font-weight:600">{row["Client"]}</span>
            <span style="color:#94a3b8;font-size:12px">{row["Tracked"]:.1f}h</span>
          </div>
          <div style="height:22px;background:#0f1117;border-radius:6px;overflow:hidden">
            <div style="height:100%;width:{pct:.1f}%;background:linear-gradient(90deg,#6366f1,#818cf8);border-radius:6px"></div>
          </div>
        </div>'''
 
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tiderise Client Utilization Report — {date_range}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0f1117;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:32px;min-height:100vh}}
  .container{{max-width:1400px;margin:0 auto}}
  table{{width:100%;border-collapse:collapse}}
  th{{padding:10px 14px;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;text-align:left;border-bottom:1px solid #2a2d3a}}
</style>
</head>
<body>
<div class="container">
 
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:32px">
    <div>
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:4px">
        <div style="width:44px;height:44px;background:linear-gradient(135deg,#6366f1,#818cf8);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:white">CU</div>
        <div>
          <h1 style="font-size:28px;font-weight:700">Tiderise Client Utilization Report</h1>
          <div style="color:#94a3b8;font-size:14px;margin-top:2px">Weekly Client Assignment Tracking — Clockify API (live)</div>
        </div>
      </div>
    </div>
    <div style="background:#1a1d27;border:1px solid #2a2d3a;padding:10px 18px;border-radius:10px;text-align:right">
      <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.5px">Reporting</div>
      <div style="color:#e2e8f0;font-size:14px;font-weight:600;margin-top:2px">{date_range}</div>
      <div style="color:#64748b;font-size:11px;margin-top:2px">Generated: {gen_time}</div>
    </div>
  </div>
 
  {kpi_cards}
  {gauges}
 
  <div style="margin-bottom:24px">
    {health_cards}
  </div>
 
  <div style="display:grid;grid-template-columns:1.8fr 1fr;gap:16px;margin-bottom:24px">
    <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
      <div style="color:#e2e8f0;font-size:16px;font-weight:700;margin-bottom:16px;text-transform:uppercase;letter-spacing:0.5px">Utilization by Client & Project</div>
      <div style="max-height:700px;overflow-y:auto">
      <table>
        <thead><tr>
          <th>Client / Project</th>
          <th style="text-align:right">Scheduled (h)</th>
          <th style="text-align:right">Tracked (h)</th>
          <th style="text-align:right">Variance</th>
          <th style="text-align:right">Utilization</th>
          <th style="text-align:right">Status</th>
        </tr></thead>
        <tbody>{client_rows}</tbody>
      </table>
      </div>
    </div>
    <div style="background:#1a1d27;border-radius:16px;padding:24px;border:1px solid #2a2d3a">
      <div style="color:#e2e8f0;font-size:16px;font-weight:700;margin-bottom:16px;text-transform:uppercase;letter-spacing:0.5px">Top Clients by Tracked Hours</div>
      {bar_rows}
    </div>
  </div>
 
  <div style="color:#64748b;font-size:11px;text-align:center;margin-top:20px;padding:16px">
    Tiderise Client Utilization Report · {date_range} · Generated {gen_time}<br>
    Source: Clockify API (live) · Internal Tiderise MW hours excluded · Health thresholds: Healthy &ge;80%, Attention 65-79%, At Risk &lt;65%
  </div>
 
</div>
</body>
</html>'''
    return html
 
 
# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", choices=["current", "previous"], default="previous",
                        help="Which week to report on (default: previous Mon-Sun)")
    parser.add_argument("--output-dir", default="./reports",
                        help="Directory to write the HTML report into (default: ./reports)")
    args = parser.parse_args()
 
    df, start_date, end_date = fetch_assignments_dataframe(args.week)
    data = build_report(df)
    html = generate_html(data, start_date, end_date)
 
    week_label = f"{start_date.strftime('%Y-%m-%d')}_to_{end_date.strftime('%Y-%m-%d')}"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"Weekly_Client_Utilization_{week_label}.html"
 
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
 
    # Human-readable log -> stderr
    print(f"Report saved: {output_path}", file=sys.stderr)
    print(f"Overall utilization: {data['overall_util']:.1f}%", file=sys.stderr)
    print(
        f"Clients: {data['num_clients']} total — {data['healthy']} healthy, "
        f"{data['attention']} attention, {data['at_risk']} at risk, "
        f"{data['zero_tracked']} zero tracked",
        file=sys.stderr,
    )
 
    # Machine-readable summary -> stdout (for Claude prompt to parse)
    cs = data['client_summary']
    summary = {
        "report_path": str(output_path).replace("\\", "/"),
        "report_filename": output_path.name,
        "date_range": f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}",
        "start_date": start_date.strftime('%Y-%m-%d'),
        "end_date": end_date.strftime('%Y-%m-%d'),
        "overall_utilization": round(data['overall_util'], 1),
        "tracked_hours": round(data['total_tracked'], 1),
        "scheduled_hours": round(data['total_scheduled'], 1),
        "total_clients": int(data['num_clients']),
        "clients_with_hours": int((cs['Tracked'] > 0).sum()) if len(cs) else 0,
        "healthy_count": int(data['healthy']),
        "attention_count": int(data['attention']),
        "at_risk_count": int(data['at_risk']),
        "zero_count": int(data['zero_tracked']),
    }
    print("===SUMMARY===")
    print(json.dumps(summary))
    return output_path
 
 
if __name__ == '__main__':
    main()