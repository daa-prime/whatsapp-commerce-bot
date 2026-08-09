# portal/dashboard.py
"""
Merchant portal dashboard (GET /portal/dashboard) -- stat tiles, a 7-day
sales trend, a payment-method breakdown, top categories, and a recent-orders
table. All the number-crunching lives in db.repository.get_dashboard_stats
(one tenant-scoped call); this module only turns those numbers into the
inline SVG/CSS chart geometry Jinja2 renders -- no client-side charting
library, consistent with this repo's "minimal maintenance" philosophy
(SPEC.md Section 1) and keeps the charts plain server-rendered HTML.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from portal.session import require_session

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_DONUT_COLORS = ["#D6006F", "#A50057", "#F48FB1", "#6B6470", "#FDE7F1", "#7A1F1A"]

_CHART_WIDTH = 520
_CHART_HEIGHT = 140
_CHART_PAD = 16


def _trend_polyline(weekly_trend: list[dict]) -> str:
    totals = [float(d["total"]) for d in weekly_trend]
    max_total = max(totals) or 1.0
    n = len(weekly_trend)
    usable_w = _CHART_WIDTH - 2 * _CHART_PAD
    usable_h = _CHART_HEIGHT - 2 * _CHART_PAD
    points = []
    for i, total in enumerate(totals):
        x = _CHART_PAD + (i * usable_w / (n - 1) if n > 1 else 0)
        y = _CHART_PAD + usable_h - (total / max_total) * usable_h
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _trend_points_with_labels(weekly_trend: list[dict]) -> list[dict]:
    totals = [float(d["total"]) for d in weekly_trend]
    max_total = max(totals) or 1.0
    n = len(weekly_trend)
    usable_w = _CHART_WIDTH - 2 * _CHART_PAD
    usable_h = _CHART_HEIGHT - 2 * _CHART_PAD
    out = []
    for i, d in enumerate(weekly_trend):
        x = _CHART_PAD + (i * usable_w / (n - 1) if n > 1 else 0)
        y = _CHART_PAD + usable_h - (float(d["total"]) / max_total) * usable_h
        out.append({"x": round(x, 1), "y": round(y, 1), "label": d["label"], "total": d["total"]})
    return out


def _donut_gradient(breakdown: list[dict]) -> str:
    total = sum(d["count"] for d in breakdown) or 1
    stops = []
    running = 0.0
    for i, d in enumerate(breakdown):
        color = _DONUT_COLORS[i % len(_DONUT_COLORS)]
        start_pct = running / total * 100
        running += d["count"]
        end_pct = running / total * 100
        stops.append(f"{color} {start_pct:.2f}% {end_pct:.2f}%")
    return ", ".join(stops)


def _donut_legend(breakdown: list[dict]) -> list[dict]:
    total = sum(d["count"] for d in breakdown) or 1
    return [
        {
            "method": d["method"],
            "count": d["count"],
            "pct": round(d["count"] / total * 100),
            "color": _DONUT_COLORS[i % len(_DONUT_COLORS)],
        }
        for i, d in enumerate(breakdown)
    ]


def _delta_css_class(pct: int | None) -> str:
    if pct is None:
        return "flat"
    return "up" if pct >= 0 else "down"


@router.get("/portal/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    stats = db.get_dashboard_stats(tenant.id, tenant.timezone)

    max_category_revenue = max((float(c["revenue"]) for c in stats["top_categories"]), default=0.0) or 1.0
    top_categories = [
        {**c, "pct": round(float(c["revenue"]) / max_category_revenue * 100)}
        for c in stats["top_categories"]
    ]

    return templates.TemplateResponse(request, "portal/dashboard.html", {
        "tenant": tenant,
        "active_nav": "dashboard",
        "stats": stats,
        "sales_delta_class": _delta_css_class(stats["sales_delta_pct"]),
        "orders_delta_class": _delta_css_class(stats["orders_delta_pct"]),
        "trend_polyline": _trend_polyline(stats["weekly_trend"]),
        "trend_points": _trend_points_with_labels(stats["weekly_trend"]),
        "chart_width": _CHART_WIDTH,
        "chart_height": _CHART_HEIGHT,
        "donut_gradient": _donut_gradient(stats["payment_method_breakdown"]),
        "donut_legend": _donut_legend(stats["payment_method_breakdown"]),
        "top_categories": top_categories,
    })
