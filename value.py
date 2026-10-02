"""Value check: what an IPO looks worth from its own data vs what the market is paying.

Everything here is computed from data already collected (issue price, GMP history, subscription,
PE, listing prices, daily closes). Nothing is fetched, nothing is guessed about the future: the
model is a transparent set of rules so every number on the screen can be traced back.

The idea, in the user's words: some issues list flat or negative even though their data (GMP,
subscription, valuation) is good, and they often move 10-30% up within the first week. So the desk
looks for shares that trade *below what their own data implies* and prints a three-tranche entry
ladder, trim levels and a stop loss for each one.

HOW THE NUMBERS ARE MADE
  expected %   = 0.55 * market expectation (peak GMP %)  +  0.45 * median listing gain of similar
                 recent issues on the same board (same GMP bucket), then multiplied by a
                 subscription factor (QIB demand) and a valuation factor (issue PE vs board median).
  fair value   = issue price * (1 + expected %)        (pre-listing expectation)
                 for listed shares the same fair value is kept as the "data value" - the level the
                 issue was worth when its data was at its best.
  gap %        = (fair - price now) / price now * 100  -> the bigger, the more "below its data"
  entry ladder = price now (or fair if cheaper) and two steps 6% and 12% lower
  stop loss    = 7% under the last tranche, or 10% under the listing price, whichever is lower
  trims        = fair +5% / +15% / +30%

Scores are a weighted opinion, not a promise. This file never raises: a bad row is simply skipped.
"""
from statistics import median

try:
    from util import num
except Exception:                                     # pragma: no cover - import safety
    def num(v):
        try:
            f = float(v)
            return f
        except (TypeError, ValueError):
            return None

BOARDS = ("Mainboard", "SME")
LISTED = {"listed", "listing_today"}
PRE = {"upcoming", "open", "closing_today", "closed", "allotment_today"}
SCOPE = PRE | LISTED | {"allotted"}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _pn(v):
    """percent of a price vs issue price, None-safe"""
    return None if v is None else round(v, 2)


def _bucket_gain(buckets, gmp_pct):
    for b in buckets:
        if b["lo"] <= (gmp_pct if gmp_pct is not None else 0) < b["hi"]:
            return b["gain_med"]
    return None


def _buckets(rows):
    """Median listing gain of listed issues, grouped by how hot their GMP was."""
    edges = [(-1e9, -10), (-10, 0), (0, 5), (5, 15), (15, 30), (30, 60), (60, 1e9)]
    out = []
    for lo, hi in edges:
        g = [d["listing_gain"] for d in rows
             if isinstance(d.get("gmp_pct"), (int, float)) and lo <= d["gmp_pct"] < hi
             and isinstance(d.get("listing_gain"), (int, float))]
        if g:
            out.append({"lo": lo, "hi": hi, "n": len(g), "gain_med": round(median(g), 2)})
    return out


def board_stats(docs):
    """Reference numbers the model calibrates against - all from the desk's own history."""
    listed = [d for d in docs if d.get("status") == "listed"]
    out = {}
    for b in BOARDS + ("all",):
        rows = listed if b == "all" else [d for d in listed if (d.get("board") or "Mainboard") == b]
        gains = [d["listing_gain"] for d in rows if isinstance(d.get("listing_gain"), (int, float))]
        cur = [d["current_gain"] for d in rows if isinstance(d.get("current_gain"), (int, float))]
        pes = [d["pe"] for d in rows if isinstance(d.get("pe"), (int, float)) and 0 < d["pe"] < 500]
        out[b] = {
            "n": len(rows),
            "gain_med": round(median(gains), 2) if gains else None,
            "gain_pos_pct": round(100 * sum(1 for g in gains if g > 0) / len(gains), 1) if gains else None,
            "cur_med": round(median(cur), 2) if cur else None,
            "pe_med": round(median(pes), 1) if pes else None,
            "gmp_buckets": _buckets(rows),
        }
    # the user's own thesis, measured on our history: weak listing -> later recovery
    weak = [d for d in listed if isinstance(d.get("listing_close_gain"), (int, float)) and d["listing_close_gain"] < 0]
    rec = [d for d in weak if isinstance(d.get("current_gain"), (int, float)) and d["current_gain"] >= 10]
    up10 = [d for d in listed if isinstance(d.get("current_gain"), (int, float)) and d["current_gain"] >= 10]
    out["recovery"] = {
        "n_weak": len(weak), "n_recovered": len(rec),
        "pct": round(100 * len(rec) / len(weak), 1) if weak else None,
        "n_up10": len(up10), "n_listed": len(listed),
    }
    return out


def _factors(d, st_all):
    """(subscription factor, valuation factor, notes)"""
    notes, sub_f, val_f = [], 1.0, 1.0
    qib = ((d.get("subx") or {}).get("share") or {}).get("qib")
    if qib is None:
        qib = (d.get("sub") or {}).get("qib")
    if isinstance(qib, (int, float)):
        if qib >= 50:
            sub_f, _n = 1.12, notes.append(f"QIB {qib:.0f}x - very strong institutional demand")
        elif qib >= 10:
            sub_f, _n = 1.07, notes.append(f"QIB {qib:.0f}x - strong institutional demand")
        elif qib >= 3:
            sub_f, _n = 1.02, notes.append(f"QIB {qib:.1f}x")
        elif qib >= 1:
            sub_f, _n = 0.98, notes.append(f"QIB only {qib:.1f}x")
        else:
            sub_f, _n = 0.92, notes.append(f"QIB {qib:.2f}x - institutions stayed away")
    pe, pe_med = d.get("pe"), (st_all or {}).get("pe_med")
    if isinstance(pe, (int, float)) and pe > 0 and isinstance(pe_med, (int, float)) and pe_med > 0:
        ratio = pe / pe_med
        if ratio <= 0.7:
            val_f, _n = 1.10, notes.append(f"issue PE {pe:.1f} vs board median {pe_med:.1f} - cheap")
        elif ratio <= 1.0:
            val_f, _n = 1.04, notes.append(f"issue PE {pe:.1f} vs board median {pe_med:.1f}")
        elif ratio <= 1.6:
            val_f, _n = 0.97, notes.append(f"issue PE {pe:.1f} vs board median {pe_med:.1f} - a bit rich")
        elif ratio <= 2.5:
            val_f, _n = 0.9, notes.append(f"issue PE {pe:.1f} vs board median {pe_med:.1f} - expensive")
        else:
            val_f, _n = 0.82, notes.append(f"issue PE {pe:.1f} vs board median {pe_med:.1f} - very expensive")
    return sub_f, val_f, notes


def evaluate(d, st, fundamentals=None):
    """One IPO -> the value-check row. Returns None when there is nothing to stand on."""
    key, status = d.get("key"), d.get("status")
    if not key or status not in SCOPE:
        return None
    board = d.get("board") or "Mainboard"
    sb = st.get(board) or st.get("Mainboard") or {}
    sa = st.get("all") or {}
    issue = num(d.get("issue_price")) or num(d.get("price_high")) or num(d.get("price_low"))
    if not issue:
        return None
    listed = status in LISTED
    price = num(d.get("current_price")) if listed else None
    listing_price = num(d.get("listing_price"))
    gmp = num(d.get("gmp"))
    gmp_pct = num(d.get("gmp_pct"))
    if gmp_pct is None and gmp is not None and issue:
        gmp_pct = round(gmp / issue * 100, 2)
    peak_gmp_pct = None
    if gmp is not None and num(d.get("gmp_high")) is not None:
        peak_gmp_pct = round(max(gmp, num(d["gmp_high"])) / issue * 100, 2)
    peak_gmp_pct = max([x for x in (peak_gmp_pct, gmp_pct, num(d.get("final_gmp")) and round(num(d["final_gmp"]) / issue * 100, 2)) if x is not None] or [None]) \
        if any(x is not None for x in (peak_gmp_pct, gmp_pct, d.get("final_gmp"))) else None

    # ---- expected move, in % over the issue price ------------------------------------
    # Deliberately conservative: grey-market peaks are noisy, so the *final* GMP is capped,
    # and the blend leans on what similar issues actually did rather than on hype.
    FINAL_GMP_CAP, EXP_CAP, EXP_FLOOR = 45.0, 40.0, -18.0
    gmp_ref = clamp(gmp_pct if gmp_pct is not None else (peak_gmp_pct or 0.0), -20.0, FINAL_GMP_CAP)
    bucket = _bucket_gain(sb.get("gmp_buckets") or [], gmp_pct if gmp_pct is not None else peak_gmp_pct)
    ref_med = bucket if bucket is not None else sb.get("gain_med")
    peer_ref = clamp(ref_med if ref_med is not None else 0.0, -25.0, 45.0)
    cur_ref = clamp(num(sa.get("cur_med")) or 0.0, -25.0, 45.0)
    if listed:
        base = 0.34 * gmp_ref + 0.46 * peer_ref + 0.20 * cur_ref
    else:
        base = 0.50 * gmp_ref + 0.50 * peer_ref
    sub_f, val_f, notes = _factors(d, sa)
    try:
        expected = clamp(base * sub_f * val_f, EXP_FLOOR, EXP_CAP)
    except TypeError:
        expected = clamp(base, EXP_FLOOR, EXP_CAP)

    # what the issue was worth when its data was at its best - never more than +60% of issue
    fair = round(issue * (1 + expected / 100), 2)
    fair = min(fair, round(issue * 1.6, 2))
    if listed and price is None and listing_price:            # no live quote: fall back to last close
        price = num(d.get("listing_close")) or listing_price
    now = price if listed and price else fair

    gap = round((fair - now) / now * 100, 1) if now else None
    below_issue = round((now - issue) / issue * 100, 1) if (now and issue) else None
    suspect = gap is not None and gap > 55          # a gap this big usually means bad/stale data

    # ---- score --------------------------------------------------------------------
    score, risks = 50.0, []
    if gap is not None and not suspect:
        score += clamp(gap * 1.0, -20, 28)
    elif suspect:
        score -= 8
        risks.append("price is far under the data value - treat the gap as unreliable until the "
                     "next few closes confirm it")
    qib = ((d.get("subx") or {}).get("share") or {}).get("qib")
    if qib is None:
        qib = (d.get("sub") or {}).get("qib")
    if isinstance(qib, (int, float)):
        score += 12 if qib >= 50 else 8 if qib >= 10 else 4 if qib >= 3 else 0 if qib >= 1 else -6
    else:
        score -= 3
    pe, pe_med = d.get("pe"), sa.get("pe_med")
    if isinstance(pe, (int, float)) and pe > 0 and isinstance(pe_med, (int, float)) and pe_med > 0:
        ratio = pe / pe_med
        score += 8 if ratio <= 0.8 else 4 if ratio <= 1.1 else -6 if ratio <= 2 else -14
    size = num(d.get("size_cr"))
    if size is not None:
        if size >= 1000:
            score += 5
        elif size >= 200:
            score += 2
        elif size < 20:
            score -= 9
            risks.append("very small issue - thin trading, wide spreads")
        elif size < 50:
            score -= 4
    if listed and listing_price and now:
        off_high = (now - listing_price) / listing_price * 100
        if -35 <= off_high <= -12:
            score += 7
            notes.append(f"holding {abs(off_high):.0f}% under its listing price")
        elif off_high < -35:
            score -= 6
            risks.append(f"down {abs(off_high):.0f}% from its listing price - a broken trend, "
                         "only the first tranche belongs here")
        elif off_high >= 60:
            score -= 8
            notes.append(f"already {off_high:.0f}% above its listing price - late to chase")
    if below_issue is not None and below_issue <= -45 and (size or 0) < 60:
        score -= 8
        risks.append("deep fall on a small issue - wait for a higher low before the first tranche")
    if below_issue is not None and below_issue <= -65:
        score -= 6
        risks.append("far below its issue price - data value is only a reference, not a target")
    elif not listed and peak_gmp_pct is not None and peak_gmp_pct >= 25:
        notes.append(f"grey market peak was {peak_gmp_pct:.0f}% over issue")
    if d.get("status") == "withdrawn":
        return None
    score = clamp(score, 0, 100)
    evidence = (isinstance(qib, (int, float)) and qib >= 3) or \
               (isinstance(pe, (int, float)) and isinstance(pe_med, (int, float)) and pe <= pe_med)
    if score >= 68 and (gap or 0) >= 10 and evidence and not suspect:
        verdict = "Below its data"
    elif score >= 62 and (gap or 0) >= 8 and not suspect:
        verdict = "Watch"
    elif score >= 45:
        verdict = "Weak"
    else:
        verdict = "Avoid"

    # ---- trade plan (only meaningful once the share trades) ------------------------
    plan = None
    if listed and price:
        anchor = round(min(price, fair * 0.98), 2)
        t = [anchor, round(anchor * 0.94, 2), round(anchor * 0.88, 2)]
        stop = round(min(t[2] * 0.93, (listing_price * 0.90) if listing_price else t[2] * 0.93), 2)
        # trims sit at the data value itself and a little beyond - not at multiples of it
        sell = [round(fair, 2), round(fair * 1.12, 2), round(fair * 1.26, 2)]
        plan = {"tranches": t, "stop": stop, "sell": sell,
                "risk_per_share": round(t[0] - stop, 2) if t[0] > stop else None}
    if gap is not None and gap >= 10:
        notes.insert(0, f"trades ~{gap:.0f}% below the value its own data implies")
    if risks:
        notes.extend(risks)

    return {
        "key": key, "name": d.get("name"), "short": d.get("short"), "board": board,
        "status": status, "status_label": d.get("status_label"),
        "issue": round(issue, 2), "price": round(now, 2) if now else None, "below_issue": below_issue,
        "suspect": bool(suspect), "risks": risks[:3],
        "fair": fair, "gap_pct": gap, "expected_pct": round(expected, 1),
        "score": round(score), "verdict": verdict,
        "qib": round(qib, 2) if isinstance(qib, (int, float)) else None,
        "sub_total": num(d.get("sub_total")), "pe": num(pe), "pe_med": pe_med,
        "size_cr": size, "gmp_pct": gmp_pct, "peak_gmp_pct": peak_gmp_pct,
        "listing_gain": num(d.get("listing_gain")), "current_gain": num(d.get("current_gain")),
        "plan": plan, "notes": notes[:6],
        "fund": fundamentals or {},
        "as_of": d.get("current_as_of") or d.get("gmp_updated"),
    }


def build(docs, fundamentals=None, today=None):
    """All rows + the calibration stats the dashboard shows."""
    st = board_stats(docs)
    fmap = fundamentals or {}
    rows = []
    for d in docs:
        try:
            r = evaluate(d, st, fmap.get(d.get("key")))
            if r:
                rows.append(r)
        except Exception:                                  # never let one row break the page
            continue
    rows.sort(key=lambda r: (-(r["score"] or 0), -(r["gap_pct"] or -999)))
    picks = [r for r in rows if r["status"] in LISTED and (r["gap_pct"] or 0) >= 10 and r["score"] >= 55]
    return {
        "generated": (today.isoformat() if hasattr(today, "isoformat") else None),
        "note": ("Mechanical comparison of price vs the issue's own data - not advice, not a "
                 "recommendation. Entry ladder, trims and stop loss are fixed percentages."),
        "stats": {
            "boards": {b: {k: v for k, v in st[b].items() if k != "gmp_buckets"} for b in st if b != "recovery"},
            "recovery": st["recovery"],
            "gmp_buckets": st.get("all", {}).get("gmp_buckets") or [],
        },
        "counts": {"rows": len(rows), "picks": len(picks),
                   "below_data": sum(1 for r in rows if (r["gap_pct"] or 0) >= 10)},
        "rows": rows,
        "picks": [r["key"] for r in sorted(picks, key=lambda r: -(r["gap_pct"] or 0))],
    }


def fundamentals_from_detail(detail):
    """Small, structured slice of the company block for the comparison panel."""
    co = (detail or {}).get("company") or {}
    kp = co.get("kpis") or {}
    fin = co.get("financials") or {}
    rows = {}
    for r in (fin.get("rows") or []):
        if r and r[0]:
            rows[str(r[0]).strip()] = r[1:]

    def cell(name, i=0):
        r = rows.get(name) or []
        if len(r) <= i:
            return None
        try:
            return float(str(r[i]).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def growth(name):
        a, b = cell(name, 0), cell(name, 1)
        if a is None or not b:
            return None
        return round((a - b) / b * 100, 1)

    docs = co.get("docs") or {}
    return {
        "eps_post": kp.get("EPS (post) \u20b9"), "eps_pre": kp.get("EPS (pre) \u20b9"),
        "roe": kp.get("ROE %"), "roce": kp.get("ROCE %"), "pb": kp.get("P/B"), "nav": kp.get("NAV \u20b9"),
        "rev": cell("Total Income"), "pat": cell("Profit After Tax"),
        "rev_growth": growth("Total Income"), "pat_growth": growth("Profit After Tax"),
        "rhp": bool(docs.get("RHP")), "drhp": bool(docs.get("DRHP")),
        "peers": bool(co.get("peer_html")), "sector": co.get("sector"),
    }
