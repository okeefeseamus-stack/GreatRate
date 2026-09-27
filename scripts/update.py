"""Daily 15-year mortgage rate pipeline.

Fetches each source in config.json, compiles one headline rate (mean of daily
sources after an outlier guard), appends to docs/data/history.json, writes
docs/data/latest.json for the dashboard, pulls news, and emails an alert when
the rate crosses your threshold.

Env vars:
  FRED_API_KEY   required
  SMTP_USER, SMTP_PASS, ALERT_TO   needed for alert emails (ALERT_TO: comma-separated)
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465)
  DASHBOARD_URL  optional, linked in the email
  TEST_ALERT=true   send an alert email regardless of threshold
  BACKFILL=true     rebuild history from FRED
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import smtplib
import statistics
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA_DIR = ROOT / "docs" / "data"
HISTORY_PATH = DATA_DIR / "history.json"
LATEST_PATH = DATA_DIR / "latest.json"
STATE_PATH = ROOT / "state.json"  # alert bookkeeping, not published

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime.now(TZ)
TODAY = NOW.date().isoformat()
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
RATE_MIN, RATE_MAX = 1.0, 15.0  # sanity bounds for any parsed rate


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- FRED

def fred(series: str, start: str | None = None) -> list[tuple[str, float]]:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    params = {"series_id": series, "api_key": key, "file_type": "json", "sort_order": "asc"}
    if start:
        params["observation_start"] = start
    r = requests.get("https://api.stlouisfed.org/fred/series/observations", params=params, timeout=30)
    r.raise_for_status()
    return [
        (o["date"], float(o["value"]))
        for o in r.json()["observations"]
        if o["value"] not in (".", "")
    ]


def fred_latest(series: str, lookback_days: int = 120) -> tuple[str, float] | None:
    start = (NOW.date() - dt.timedelta(days=lookback_days)).isoformat()
    obs = fred(series, start)
    return obs[-1] if obs else None


# ---------------------------------------------------------------- scrapers

def page_text(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def first_pct_after(text: str, anchor: str, window: int = 300) -> float | None:
    i = text.find(anchor)
    if i < 0:
        return None
    m = re.search(r"(\d{1,2}\.\d{1,3})\s?%", text[i + len(anchor): i + len(anchor) + window])
    if not m:
        return None
    v = float(m.group(1))
    return v if RATE_MIN <= v <= RATE_MAX else None


def parse_mnd(text: str) -> float | None:
    # 15-year page table first, then the site-wide header ticker.
    return first_pct_after(text, "MND's 15 Year Fixed") or first_pct_after(text, "15YR Fixed Rate", 40)


def parse_tmr(text: str) -> float | None:
    return first_pct_after(text, "Conventional 15-year fixed")


PARSERS = {"mnd": parse_mnd, "tmr": parse_tmr}


# ---------------------------------------------------------------- sources

def fetch_source(src: dict) -> dict:
    out = {k: src.get(k) for k in ("id", "name", "url", "note", "in_average", "type")}
    out.update(value=None, as_of=None, error=None, excluded=False)
    try:
        if src["type"] == "fred":
            latest = fred_latest(src["series"])
            if latest:
                out["as_of"], out["value"] = latest[0], round(latest[1], 3)
        elif src["type"] == "scrape":
            v = PARSERS[src["parser"]](page_text(src["url"]))
            if v is None:
                raise ValueError("rate not found on page (layout may have changed)")
            out["value"], out["as_of"] = v, TODAY
    except Exception as e:  # one bad source never kills the run
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        log(f"  ! {src['id']}: {out['error']}")
    return out


def compile_rate(sources: list[dict]) -> float | None:
    pool = [s for s in sources if s["in_average"] and s["value"] is not None]
    if not pool:
        return None
    if len(pool) >= 3:
        med = statistics.median(s["value"] for s in pool)
        for s in pool:
            if abs(s["value"] - med) > CONFIG["outlier_pts"]:
                s["excluded"] = True
                log(f"  ~ {s['id']} excluded as outlier ({s['value']} vs median {med})")
        pool = [s for s in pool if not s["excluded"]]
    return round(statistics.mean(s["value"] for s in pool), 2)


# ---------------------------------------------------------------- history

def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {"records": []}


def backfill(hist: dict) -> dict:
    ob = next(s for s in CONFIG["sources"] if s["id"] == "optimal_blue")
    start = (NOW.date() - dt.timedelta(days=CONFIG["backfill_days"])).isoformat()
    log(f"Backfilling from FRED {ob['series']} since {start}")
    live = [r for r in hist.get("records", []) if not r.get("backfill") and not hist.get("sample")]
    live_dates = {r["date"] for r in live}
    recs = [
        {"date": d, "rate": round(v, 2), "sources": {"optimal_blue": v}, "backfill": True}
        for d, v in fred(ob["series"], start)
        if d not in live_dates
    ]
    return {"records": sorted(recs + live, key=lambda r: r["date"])}


def upsert(hist: dict, rec: dict) -> None:
    hist["records"] = [r for r in hist["records"] if r["date"] != rec["date"]] + [rec]
    hist["records"].sort(key=lambda r: r["date"])


# ---------------------------------------------------------------- metrics

def build_metrics(hist: dict, rate: float) -> dict:
    m: dict = {}
    cutoff = (NOW.date() - dt.timedelta(days=182)).isoformat()
    six = [r for r in hist["records"] if r["date"] >= cutoff]
    if six:
        hi = max(six, key=lambda r: r["rate"])
        lo = min(six, key=lambda r: r["rate"])
        m["high_6m"] = {"value": hi["rate"], "date": hi["date"]}
        m["low_6m"] = {"value": lo["rate"], "date": lo["date"]}
    for key, series in CONFIG["metrics"].items():
        try:
            latest = fred_latest(series, 400)
            if latest:
                m[key] = {"value": latest[1], "date": latest[0], "series": series,
                          "url": f"https://fred.stlouisfed.org/series/{series}"}
        except Exception as e:
            log(f"  ! metric {key}: {e}")
    if "rate_30y" in m:
        m["spread_30_15"] = {"value": round(m["rate_30y"]["value"] - rate, 2)}
    return m


# ---------------------------------------------------------------- news

def fetch_news() -> list[dict]:
    cfg = CONFIG["news"]
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": cfg["query"], "hl": "en-US", "gl": "US", "ceid": "US:en"},
            headers=UA, timeout=30,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log(f"  ! news: {e}")
        return []
    preferred = [p.lower() for p in cfg["preferred_outlets"]]
    items, seen = [], set()
    for it in root.iter("item"):
        outlet = (it.findtext("source") or "").strip()
        title = (it.findtext("title") or "").strip()
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3]
        key = title.lower()[:60]
        if not title or key in seen:
            continue
        seen.add(key)
        try:
            published = dt.datetime.strptime(it.findtext("pubDate"), "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
        except Exception:
            published = None
        items.append({
            "title": title, "url": it.findtext("link"), "outlet": outlet, "published": published,
            "preferred": outlet.lower() in preferred,
        })
    items.sort(key=lambda x: (not x["preferred"], -(int((x["published"] or "0").replace("-", "")))))
    return items[: cfg["max_items"]]


# ---------------------------------------------------------------- alerts

def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def check_alert(rate: float, prev: float | None, state: dict) -> list[str]:
    a = CONFIG["alert"]
    if not a.get("enabled"):
        return []
    # Moving the threshold re-arms the alert.
    if state.get("threshold") != a["below"]:
        state.clear()
        state["threshold"] = a["below"]
    reasons = []
    last = state.get("last_alert_date")
    cooled = not last or (NOW.date() - dt.date.fromisoformat(last)).days >= a["cooldown_days"]
    new_low = state.get("last_alert_rate") is not None and rate <= state["last_alert_rate"] - 0.05
    if rate <= a["below"] and (cooled or new_low):
        reasons.append(f"The 15-year rate is {rate:.2f}%, at or below your {a['below']:.2f}% target.")
    if prev is not None and a.get("drop_bps") and (prev - rate) * 100 >= a["drop_bps"]:
        reasons.append(f"It fell {(prev - rate):.2f} points since the last reading ({prev:.2f}%).")
    return reasons


def alert_html(rate: float, reasons: list[str], sources: list[dict]) -> str:
    rows = ""
    for s in sources:
        if s["value"] is None:
            continue
        tag = " (weekly, not averaged)" if not s["in_average"] else (" (excluded as outlier)" if s["excluded"] else "")
        rows += (
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #D6DDE4;">'
            f'<a href="{html.escape(s["url"])}" style="color:#14304A;">{html.escape(s["name"])}</a>'
            f'<span style="color:#5F6E7C;font-size:13px;">{tag}</span></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #D6DDE4;text-align:right;font-weight:600;">{s["value"]:.2f}%</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #D6DDE4;color:#5F6E7C;font-size:13px;">{s["as_of"]}</td></tr>'
        )
    dash = os.environ.get("DASHBOARD_URL", "")
    dash_link = f'<p style="margin:24px 0 0;"><a href="{html.escape(dash)}" style="color:#14304A;font-weight:600;">Open the dashboard</a></p>' if dash else ""
    reason_html = "".join(f'<p style="margin:0 0 6px;font-size:16px;">{html.escape(r)}</p>' for r in reasons)
    return f"""<!doctype html><html><body style="margin:0;background:#EEF1F4;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1B2733;">
<div style="max-width:560px;margin:0 auto;padding:24px;">
  <div style="background:#14304A;color:#fff;border-radius:20px;padding:32px 24px;text-align:center;">
    <div style="font-size:15px;opacity:.8;">15-year fixed mortgage rate</div>
    <div style="font-family:Georgia,serif;font-size:84px;line-height:1.05;font-weight:600;margin:8px 0;">{rate:.2f}%</div>
    <div style="font-size:14px;opacity:.8;">{NOW.strftime('%A, %B %-d, %Y')}</div>
  </div>
  <div style="background:#fff;border-radius:16px;padding:20px 24px;margin-top:16px;">
    {reason_html}
    <p style="margin:18px 0 8px;font-size:14px;color:#5F6E7C;">Where this number came from</p>
    <table style="width:100%;border-collapse:collapse;font-size:15px;">{rows}</table>
    <p style="margin:12px 0 0;font-size:13px;color:#5F6E7C;">Headline is the average of the daily sources above. Your quoted rate will depend on credit, points and lender.</p>
    {dash_link}
  </div>
</div></body></html>"""


def send_email(subject: str, body_html: str) -> None:
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "ALERT_TO"))
    if not (user and pw and to):
        log("  ! email skipped: SMTP_USER / SMTP_PASS / ALERT_TO not set")
        return
    recipients = [x.strip() for x in to.split(",") if x.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(recipients)
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", body_html), "plain"))
    msg.attach(MIMEText(body_html, "html"))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())
    log(f"  > alert emailed to {len(recipients)} recipient(s)")


# ---------------------------------------------------------------- main

def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hist = load_history()
    if env_flag("BACKFILL") or not hist["records"] or hist.get("sample"):
        hist = backfill(hist)

    log("Fetching sources")
    sources = [fetch_source(s) for s in CONFIG["sources"]]
    rate = compile_rate(sources)
    if rate is None:
        log("No daily source returned a rate; keeping yesterday's data.")
        return 1

    prior = [r for r in hist["records"] if r["date"] < TODAY]
    prev = prior[-1]["rate"] if prior else None
    upsert(hist, {
        "date": TODAY, "rate": rate, "backfill": False,
        "sources": {s["id"]: s["value"] for s in sources if s["value"] is not None},
    })

    metrics = build_metrics(hist, rate)
    news = fetch_news()
    used = [s for s in sources if s["in_average"] and s["value"] is not None and not s["excluded"]]

    latest = {
        "title": CONFIG["title"],
        "updated_at": NOW.isoformat(timespec="minutes"),
        "date": TODAY,
        "rate": rate,
        "previous": prev,
        "change": round(rate - prev, 2) if prev is not None else None,
        "averaged_count": len(used),
        "sources": sources,
        "metrics": metrics,
        "news": news,
    }
    HISTORY_PATH.write_text(json.dumps(hist, separators=(",", ":")))
    LATEST_PATH.write_text(json.dumps(latest, indent=1))
    log(f"Headline 15-yr: {rate:.2f}% (prev {prev}) from {len(used)} source(s)")

    state = load_state()
    reasons = check_alert(rate, prev, state)
    if env_flag("TEST_ALERT"):
        reasons = reasons or ["Test alert. Your email setup works."]
    if reasons:
        try:
            send_email(f"15-yr rate alert: {rate:.2f}%", alert_html(rate, reasons, sources))
            if not env_flag("TEST_ALERT"):
                state.update(last_alert_date=TODAY, last_alert_rate=rate)
        except Exception as e:
            log(f"  ! email failed: {e}")
    STATE_PATH.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
