"""Merge per-source payloads into one verified IPO document.

Priority rules
  * dates / price band / lot / size : NSE (official) > Narada > InvestorGain > Chittorgarh
  * GMP (unofficial by nature)      : InvestorGain > Narada   (both shown, differences flagged)
  * subscription                    : freshest snapshot wins (Narada/NSE/InvestorGain), all compared
  * listing price                   : NSE/BSE bhavcopy (official) > InvestorGain > Narada
  * current price                   : NSE live > BSE live > Narada > BSE bhavcopy > InvestorGain LTP
"""
import re
from datetime import datetime

from util import (IST, clean_html, dstr, html_table_rows, num, parse_date, pct, rnd, strip_tags,
                  today_ist)

STATUS_LABEL = {
    "upcoming": "Upcoming", "open": "Open", "closing_today": "Closing Today", "closed": "Closed",
    "allotment_today": "Allotment Today", "allotted": "Allotted", "listing_today": "Listing Today",
    "listed": "Listed", "withdrawn": "Withdrawn",
}
STATUS_ORDER = {"closing_today": 0, "open": 1, "listing_today": 2, "allotment_today": 3, "allotted": 4,
                "closed": 5, "upcoming": 6, "listed": 7, "withdrawn": 8}

IG_STATUS = {"U": "upcoming", "O": "open", "CT": "closing_today", "C": "closed", "BA": "allotment_today",
             "A": "allotted", "LP": "allotted", "LT": "listing_today", "LN": "listed", "L": "listed"}
ND_SECTION = {"UPCOMING": "upcoming", "OPEN": "open", "CLOSED": "closed", "ALLOTTED": "allotted",
              "LISTED": "listed", "CANCELLED": "withdrawn", "WITHDRAWN": "withdrawn", "LISTING": "listing_today"}


def first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _d(v):
    return parse_date(v) if v else None


def _ts(s, ref=None):
    """Parse loose timestamps like '25th Sep 12:39', '18th Sep 2026 16:07', ISO strings -> aware datetime."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}T", s):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        pass
    d = parse_date(s, ref=ref, mode="past")
    if not d:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    h, mi = (int(m.group(1)), int(m.group(2))) if m else (23, 0)
    try:
        return datetime(d.year, d.month, d.day, h, mi, tzinfo=IST)
    except ValueError:
        return None


class Verifier:
    """Collects the value each source reports for a field and whether they agree."""

    def __init__(self):
        self.checks = []

    def add(self, field, label, cands, kind="num", tol_abs=0.0, tol_rel=0.01, chosen=None, fmt=None, live=False):
        vals = [(s, v) for s, v in cands if v not in (None, "", 0) or (kind == "num" and v == 0 and field == "gmp")]
        uniq = []
        for s, v in vals:
            uniq.append({"src": s, "val": v})
        status = "none"
        if len(vals) == 1:
            status = "single"
        elif len(vals) > 1:
            base = vals[0][1]
            agree = True
            for _, v in vals[1:]:
                if kind == "num":
                    try:
                        a, b = float(base), float(v)
                    except (TypeError, ValueError):
                        agree = False
                        break
                    tol = max(tol_abs, abs(a) * tol_rel)
                    if abs(a - b) > tol + 1e-9:
                        agree = False
                        break
                else:
                    if str(base).strip().lower() != str(v).strip().lower():
                        agree = False
                        break
            status = "ok" if agree else ("live" if live else "diff")
        if status != "none":
            self.checks.append({"field": field, "label": label, "values": uniq, "status": status,
                                "chosen": chosen if chosen is not None else (vals[0][1] if vals else None),
                                "kind": kind})
        return status

    def summary(self):
        ok = sum(1 for c in self.checks if c["status"] == "ok")
        diff = sum(1 for c in self.checks if c["status"] == "diff")
        single = sum(1 for c in self.checks if c["status"] == "single")
        live = sum(1 for c in self.checks if c["status"] == "live")
        return {"ok": ok, "diff": diff, "single": single, "live": live, "total": len(self.checks),
                "diff_fields": [c["label"] for c in self.checks if c["status"] == "diff"]}


# ----------------------------------------------------------------------------- helpers
def _band(s):
    if not s:
        return None, None
    nums = [num(x) for x in re.findall(r"[\d.]+", str(s).replace(",", ""))]
    nums = [x for x in nums if x]
    if not nums:
        return None, None
    return nums[0], nums[-1]


_CAT_FIX = {"ind": "rii", "individual": "rii", "individuals": "rii", "employee": "emp", "employees": "emp"}


def _nd_norm(rows):
    out = []
    for r in rows or []:
        r = dict(r)
        r["cat"] = _CAT_FIX.get(r.get("cat"), r.get("cat"))
        out.append(r)
    return out


def _nd_times(rows):
    return {r["cat"]: r.get("times") for r in _nd_norm(rows) if r.get("times") is not None}


def _app_size_fallback(lot, price, board):
    """Mainboard SEBI slabs: RII <= ₹2L, sNII ₹2-10L, bNII > ₹10L."""
    if not lot or not price or board == "SME":
        return []
    per = lot * price
    rmax = max(1, int(200000 // per))
    smin, smax = rmax + 1, max(rmax + 1, int(1000000 // per))
    bmin = smax + 1
    mk = lambda lots: {"lots": lots, "shares": lots * lot, "amount": round(lots * per)}
    return [{"quota": "RII", "min": mk(1), "max": mk(rmax)},
            {"quota": "sNII", "min": mk(smin), "max": mk(smax)},
            {"quota": "bNII", "min": mk(bmin), "max": {"text": "No limit"}}]


def _fin_table(html):
    rows = html_table_rows(html)
    if not rows:
        return None
    out = {"header": None, "rows": []}
    for r in rows:
        if not r:
            continue
        if r[0].lower().startswith("period"):
            out["header"] = r
        elif out["header"] and len(r) >= 2 and not r[0].lower().startswith(("amount in", "company financials")):
            out["rows"].append(r)
    return out if out["rows"] else None


def compute_status(dates, hints, listed_evidence, withdrawn, today):
    o, c, a, l = (dates.get(k) for k in ("open", "close", "allotment", "listing"))
    o, c, a, l = (_d(x) for x in (o, c, a, l))
    if withdrawn:
        return "withdrawn"
    if l and today > l:
        return "listed"
    if l and today == l:
        return "listing_today"
    if listed_evidence and (not l or l <= today):
        return "listed"
    if a and today == a:
        return "allotment_today"
    if a and today > a:
        return "allotted"
    if c and today > c:
        return "closed"
    if c and today == c and (not o or o <= today):
        return "closing_today"
    if o and o <= today and (not c or today < c):
        return "open"
    if o and today < o:
        return "upcoming"
    for h in hints:
        if h:
            return h
    return "upcoming"


# ----------------------------------------------------------------------------- main merge
def build_doc(key, raw, gmp_rows, sub_snaps, today=None):
    today = today or today_ist()
    cat = raw.get("ig_cat") or {}
    live = raw.get("ig_live") or {}
    sublive = raw.get("ig_sublive") or {}
    perf = raw.get("ig_perf") or {}
    igd = raw.get("ig_detail") or {}
    ig = igd.get("ipo") or {}
    igs = raw.get("ig_sub") or {}
    ndl = raw.get("nd_list") or {}
    ndd = raw.get("nd_detail") or {}
    nse = raw.get("nse_list") or {}
    nsec = raw.get("nse_cat") or {}
    cg = raw.get("cg") or {}
    q_nse = raw.get("nse_quote") or {}
    q_bse = raw.get("bse_quote") or {}
    bhav_l = raw.get("bse_bhav_listing") or {}
    bhav_last = raw.get("bse_bhav_last") or {}
    nbhav_l = raw.get("nse_bhav_listing") or {}
    V = Verifier()

    # ------------------------------------------------------------ identity
    name = first(strip_tags(ig.get("company_name")), ndd.get("name"), ndl.get("name"),
                 live.get("name") and live["name"] + " Ltd.", cat.get("name"), nse.get("name"), perf.get("name"))
    short = first(strip_tags(ig.get("company_short_name")), live.get("name"), cat.get("name"), perf.get("name"),
                  re.sub(r"\s+(Limited|Ltd\.?)$", "", name or "", flags=re.I))
    ig_board = {"SME": "SME", "Mainline": "Mainboard"}.get(ig.get("issue_category"))
    board = first(ig_board, live.get("board"), perf.get("board"), ndl.get("board"), nse.get("board"), "Mainboard")
    exchange = first(ig.get("ipo_listing_at"), live.get("exchange"),
                     ndd.get("exchanges") and ndd["exchanges"].replace(" ", ", "))
    perf_sym = perf.get("symbol") or ""
    perf_nse = next((p.strip() for p in perf_sym.split(",") if p.strip() and not p.strip().isdigit()), None)
    perf_bse = next((p.strip() for p in perf_sym.split(",") if p.strip().isdigit()), None)
    nse_sym = first(ig.get("nse_symbol"), ig.get("nse_script_symbol"), ig.get("nse_cd"), perf_nse,
                    nse.get("symbol"))
    bse_code = first(str(ig.get("bse_script_code") or "") or None, str(ig.get("bse_cd") or "") or None, perf_bse)
    if bse_code and not str(bse_code).isdigit():
        bse_code = None
    nd_sym = first(ndd.get("sym"), ndl.get("sym"))
    cg_id = first(ig.get("cor_id"), cat.get("cor_id"))
    slug = first(ig.get("urlrewrite_folder_name"), cat.get("slug"), live.get("slug"))
    ig_id = first(ig.get("id"), cat.get("ig_id"), live.get("ig_id"), perf.get("ig_id"))

    # ------------------------------------------------------------ price / lot / size
    ig_lo, ig_hi = first(num(ig.get("issue_price_lower")), _band(ig.get("issue_price"))[0]), \
        first(num(ig.get("issue_price_upper")), _band(ig.get("issue_price"))[1])
    nd_lo, nd_hi = first(ndd.get("price_low"), ndl.get("price_low")), first(ndd.get("price_high"), ndl.get("price_high"))
    price_high = first(nse.get("price_high"), nd_hi, ig_hi, live.get("price"), perf.get("issue_price"),
                       ndd.get("issue_price"), ndl.get("issue_price"))
    price_low = first(nse.get("price_low"), nd_lo, ig_lo, price_high)
    V.add("price_high", "Price band (upper)", [("NSE", nse.get("price_high")), ("Narada", nd_hi),
                                               ("InvestorGain", ig_hi), ("InvestorGain live", live.get("price"))],
          chosen=price_high)
    issue_price = first(num(ig.get("issue_price_final")) if num(ig.get("issue_price_final")) else None,
                        num(ig.get("allotment_price")), perf.get("issue_price"), ndd.get("issue_price"),
                        ndl.get("issue_price"))
    listed_like = bool(first(num(ig.get("listing_price")), perf.get("listing_price"), ndd.get("listing_price"),
                             ndl.get("listing_price")))
    if listed_like:
        V.add("issue_price", "Final issue price", [("InvestorGain", num(ig.get("issue_price_final")) or num(ig.get("allotment_price"))),
                                                   ("IG tracker", perf.get("issue_price")),
                                                   ("Narada", first(ndd.get("issue_price"), ndl.get("issue_price")))],
              chosen=issue_price)
    ref_price = first(issue_price if listed_like else None, price_high)
    lot = first(num(ig.get("market_lot_size")), ndd.get("lot"), live.get("lot"))
    V.add("lot", "Lot size (shares)", [("InvestorGain", num(ig.get("market_lot_size"))), ("Narada", ndd.get("lot")),
                                       ("InvestorGain live", live.get("lot"))], tol_rel=0, chosen=lot)
    ig_size = num(ig.get("issue_size"))
    size_cr = first(ig_size, ndd.get("size_cr"), live.get("size_cr"), perf.get("size_cr"))
    V.add("size_cr", "Issue size (₹ Cr)", [("InvestorGain", ig_size), ("Narada", ndd.get("size_cr")),
                                           ("InvestorGain live", live.get("size_cr")), ("IG tracker", perf.get("size_cr"))],
          tol_rel=0.01, chosen=size_cr)
    fresh_cr, ofs_cr = num(ig.get("fresh_issue")), num(ig.get("ofs"))

    # ------------------------------------------------------------ dates
    tl = ndd.get("timeline") or {}
    dates = {
        "open": first(nse.get("open"), tl.get("Opens"), dstr(_d(ig.get("issue_open_date"))), live.get("open"), cat.get("open")),
        "close": first(nse.get("close"), tl.get("Closes"), dstr(_d(ig.get("issue_close_date"))), live.get("close"), cat.get("close")),
        "allotment": first(tl.get("Allotment"), dstr(_d(ig.get("timetable_boa_dt"))), live.get("allotment")),
        "refund": dstr(_d(ig.get("timetable_refunds_dt"))),
        "credit": first(tl.get("Settlement"), dstr(_d(ig.get("timetable_share_credit_dt")))),
        "listing": first(nse.get("listing"), tl.get("Listing"), dstr(_d(ig.get("ipo_listing_date"))),
                         dstr(_d(ig.get("timetable_listing_dt"))), live.get("listing"), perf.get("listing"),
                         ndl.get("listing_date")),
        "mandate_end": tl.get("Mandate end"),
        "anchor_bid": dstr(_d(ig.get("timetable_anchor_bid_dt"))),
        "anchor_50": first(tl.get("Anchor 50%"), dstr(_d(ig.get("timetable_anchor_lockin_end_dt_1")))),
        "anchor_100": first(tl.get("Anchor 100%"), dstr(_d(ig.get("timetable_anchor_lockin_end_dt_2")))),
    }
    if ig.get("issue_extend_close_date"):
        ext = dstr(_d(ig.get("issue_extend_close_date")))
        if ext:
            dates["close"] = first(nse.get("close"), tl.get("Closes"), ext)
    V.add("open", "Open date", [("NSE", nse.get("open")), ("Narada", tl.get("Opens")),
                                ("InvestorGain", dstr(_d(ig.get("issue_open_date"))) or live.get("open") or cat.get("open"))],
          kind="date", chosen=dates["open"])
    V.add("close", "Close date", [("NSE", nse.get("close")), ("Narada", tl.get("Closes")),
                                  ("InvestorGain", dstr(_d(ig.get("issue_close_date"))) or live.get("close") or cat.get("close"))],
          kind="date", chosen=dates["close"])
    V.add("allotment", "Allotment date", [("Narada", tl.get("Allotment")),
                                          ("InvestorGain", dstr(_d(ig.get("timetable_boa_dt"))) or live.get("allotment"))],
          kind="date", chosen=dates["allotment"])
    V.add("listing", "Listing date", [("NSE", nse.get("listing")), ("Narada", tl.get("Listing") or ndl.get("listing_date")),
                                      ("InvestorGain", dstr(_d(ig.get("ipo_listing_date"))) or live.get("listing") or perf.get("listing"))],
          kind="date", chosen=dates["listing"])

    # ------------------------------------------------------------ GMP (current)
    hist = merge_gmp_history(gmp_rows, ref_price or price_high)
    last_ig = next((h for h in reversed(hist) if h.get("ig") is not None), None)
    ig_gmp = live.get("gmp") if live.get("gmp") is not None else (last_ig or {}).get("ig")
    nd_gmp = first(ndd.get("gmp"), ndl.get("gmp")) if (ndd.get("gmp") is not None or ndl.get("gmp") is not None) else None
    if nd_gmp is None and ndd.get("gmp") == 0:
        nd_gmp = 0.0
    gmp = ig_gmp if ig_gmp is not None else nd_gmp
    if gmp is None and perf.get("final_gmp") is not None:
        gmp = perf.get("final_gmp")
    gmp_src = "InvestorGain" if ig_gmp is not None else ("Narada" if nd_gmp is not None else ("InvestorGain" if gmp is not None else None))
    tol = max(1.0, 0.02 * (price_high or 0))
    V.add("gmp", "GMP (₹)", [("InvestorGain", ig_gmp), ("Narada", nd_gmp)], tol_abs=tol, tol_rel=0, chosen=gmp)
    base = price_high or ref_price
    gmp_pct = rnd(gmp / base * 100, 2) if (gmp is not None and base) else first(live.get("gmp_pct"), ndd.get("gmp_pct"))
    est_listing = rnd(base + gmp, 2) if (gmp is not None and base) else None
    gvals = [h["gmp"] for h in hist if h.get("gmp") is not None]
    gmp_low = first(live.get("gmp_low"), min(gvals) if gvals else None)
    gmp_high = first(live.get("gmp_high"), max(gvals) if gvals else None)
    gmp_updated = first(live.get("gmp_updated"), (last_ig or {}).get("updated"))

    # ------------------------------------------------------------ subscription
    sub_cands = []
    nd_sh = _nd_norm(ndd.get("sub_shares"))
    if nd_sh:
        sub_cands.append(("Narada", _ts(ndd.get("_fetched") or ndd.get("fetched")), _nd_times(nd_sh)))
    bids = igs.get("bids") or []
    if bids:
        sub_cands.append(("InvestorGain", _ts(bids[-1].get("as_of")), {k: v for k, v in (bids[-1].get("times") or {}).items() if v}))
    if sublive.get("total") is not None:
        sub_cands.append(("InvestorGain live", _ts(sublive.get("as_of")),
                          {k: sublive.get(k) for k in ("total", "qib", "nii", "snii", "bnii", "rii") if sublive.get(k) is not None}))
    nse_rows = nsec.get("rows") or []
    if nse_rows:
        m = {}
        for r in nse_rows:
            c = (r.get("category") or "").lower()
            if "qualified" in c or "qib" in c:
                k = "qib"
            elif "retail" in c or ("individual" in c and "non" not in c):
                k = "rii"
            elif ("2" in c and "10" in c) or "two lakh" in c or "small" in c or "upto" in c:
                k = "snii"
            elif "above" in c or "more than" in c or "big" in c:
                k = "bnii"
            elif "non institutional" in c or "non-institutional" in c or "nii" in c:
                k = "nii"
            elif "employee" in c:
                k = "emp"
            elif "shareholder" in c:
                k = "shareholder"
            elif "total" in c:
                k = "total"
            else:
                k = None
            if k and r.get("times") is not None and k not in m:
                m[k] = r["times"]
        if m:
            sub_cands.append(("NSE", _ts(nsec.get("as_of")) or _ts(nsec.get("_fetched")), m))
    status_hint_open = bool(dates.get("open") and _d(dates["open"]) and _d(dates["open"]) <= today)
    chosen_sub = None
    if sub_cands:
        epoch = datetime(2000, 1, 1, tzinfo=IST)
        chosen_sub = max(sub_cands, key=lambda c: ((c[1] or epoch), c[0] == "Narada"))
    sub = dict(chosen_sub[2]) if chosen_sub else {}
    sub_total = first(sub.get("total"), live.get("sub_total"), ndl.get("sub_total"), perf.get("sub_total"),
                      nse.get("sub_total"))
    if sub_total is not None:
        sub["total"] = sub_total
    is_live = _d(dates.get("close")) is not None and _d(dates.get("open")) is not None and \
        _d(dates["open"]) <= today <= _d(dates["close"])
    V.add("sub_total", "Subscription total (x)",
          [(c[0], c[2].get("total")) for c in sub_cands] + [("InvestorGain list", live.get("sub_total")),
                                                            ("Narada list", ndl.get("sub_total")),
                                                            ("NSE list", nse.get("sub_total"))],
          tol_rel=0.05, tol_abs=0.02, chosen=sub_total, live=is_live)
    if sub.get("rii") is not None:
        V.add("sub_rii", "Retail subscription (x)", [(c[0], c[2].get("rii")) for c in sub_cands] +
              [("Chittorgarh", (cg.get("times") or {}).get("rii"))], tol_rel=0.05, tol_abs=0.02, chosen=sub.get("rii"),
              live=is_live)

    # day-wise (share based) from InvestorGain
    daywise = []
    for i, b in enumerate(bids):
        daywise.append({"day": i + 1, "as_of": b.get("as_of"), "times": b.get("times"),
                        "amount_cr": (b.get("amount_cr") or {}).get("total")})
    last_bid = bids[-1] if bids else None

    # share-wise table
    shares_rows = []
    if nd_sh:
        for r in nd_sh:
            shares_rows.append({"cat": r["cat"], "label": r["label"], "offered_cr": r.get("a"), "applied_cr": r.get("b"),
                                "times": r.get("times"), "src": "Narada"})
    ig_rows = []
    if last_bid:
        lab = {"qib": "QIB", "nii": "NII", "bnii": "bNII (> ₹10L)", "snii": "sNII (₹2-10L)", "rii": "RII",
               "emp": "Employee", "shareholder": "Shareholder", "other": "Others", "total": "Total"}
        for k in ("qib", "nii", "bnii", "snii", "rii", "emp", "shareholder", "other", "total"):
            off = (last_bid.get("offered") or {}).get(k)
            bid = (last_bid.get("bid") or {}).get(k)
            if not off and not bid:
                continue
            ig_rows.append({"cat": k, "label": lab[k], "offered": off, "bid": bid,
                            "times": (last_bid.get("times") or {}).get(k), "amount_cr": (last_bid.get("amount_cr") or {}).get(k)})

    # application-wise table
    apps_rows = []
    nd_ap = _nd_norm(ndd.get("sub_apps"))
    for r in nd_ap:
        reserved = r.get("a") if r["cat"] not in ("qib", "total", "fii", "dfi", "mf", "other_qib", "anchor") else None
        received = r.get("b")
        times = r.get("times") if reserved else None
        apps_rows.append({"cat": r["cat"], "label": r["label"], "reserved": reserved, "received": received,
                          "times": times})
    if apps_rows:   # recompute a meaningful total: individual categories only
        tot = next((r for r in apps_rows if r["cat"] == "total"), None)
        if tot:
            indiv = [r for r in apps_rows if r["cat"] in ("rii", "snii", "bnii", "emp", "shareholder")]
            res = sum(r["reserved"] or 0 for r in indiv if r["cat"] != "nii")
            rec = sum(r["received"] or 0 for r in indiv if r["cat"] != "nii")
            tot["reserved_individual"] = res or None
            tot["received_individual"] = rec or None
            tot["times"] = rnd(rec / res, 2) if res else None
    boa = cg.get("boa") or []
    total_apps = first(next((r.get("received") for r in apps_rows if r["cat"] == "total"), None),
                       cg.get("total_applications"))
    V.add("total_apps", "Total applications", [("Narada", next((r.get("received") for r in apps_rows if r["cat"] == "total"), None)),
                                               ("Chittorgarh", cg.get("total_applications"))], tol_rel=0.05, chosen=total_apps,
          live=is_live)

    # ------------------------------------------------------------ listing & current price
    on_nse = "NSE" in str(exchange or "").upper() or bool(nse_sym and not str(nse_sym).isdigit() and "BSE SME" not in str(exchange or ""))
    bse_ref = not on_nse          # BSE-only issues: BSE bhavcopy is the official listing record
    listing_price = first(nbhav_l.get("open"), bhav_l.get("open") if bse_ref else None, num(ig.get("listing_price")),
                          perf.get("listing_price"), ndd.get("listing_price"), ndl.get("listing_price"), bhav_l.get("open"))
    V.add("listing_price", "Listing price (₹)", [("NSE bhavcopy", nbhav_l.get("open")),
                                                 ("BSE bhavcopy", bhav_l.get("open") if bse_ref else None),
                                                 ("InvestorGain", num(ig.get("listing_price"))), ("IG tracker", perf.get("listing_price")),
                                                 ("Narada", first(ndd.get("listing_price"), ndl.get("listing_price")))],
          tol_rel=0.01, chosen=listing_price)
    listing_close = first(nbhav_l.get("close"), bhav_l.get("close") if bse_ref else None, perf.get("listing_day_close"),
                          cg.get("listing_day_close"), bhav_l.get("close"))
    V.add("listing_close", "Listing-day close (₹)", [("NSE bhavcopy", nbhav_l.get("close")),
                                                     ("BSE bhavcopy", bhav_l.get("close") if bse_ref else None),
                                                     ("IG tracker", perf.get("listing_day_close")),
                                                     ("Chittorgarh", cg.get("listing_day_close"))], tol_rel=0.01, chosen=listing_close)
    cur_cands = [("NSE live", q_nse.get("ltp"), q_nse.get("_fetched"), q_nse.get("prev_close")),
                 ("BSE live", q_bse.get("ltp"), q_bse.get("_fetched"), q_bse.get("prev_close")),
                 ("Narada", first(ndl.get("current_price"), ndd.get("current_price")), first(ndl.get("_fetched"), ndd.get("_fetched")), None),
                 ("BSE bhavcopy", bhav_last.get("close"), bhav_last.get("date"), bhav_last.get("prev_close")),
                 ("IG tracker", perf.get("ltp"), perf.get("updated"), None)]
    current = next((c for c in cur_cands if c[1]), None)
    listed = compute_status(dates, [], bool(listing_price), False, today) in ("listed",) or \
        (bool(listing_price) and dates.get("listing") and _d(dates["listing"]) and _d(dates["listing"]) <= today)
    if listed:
        # compare like with like: live quotes vs live quotes, closing prices vs closing prices
        from util import is_market_hours
        V.add("current_price", "Live price (₹)", [(c[0], c[1]) for c in cur_cands[:3]], tol_rel=0.02,
              live=is_market_hours())
        V.add("last_close", "Previous close (₹)", [("NSE", q_nse.get("prev_close")), ("BSE quote", q_bse.get("prev_close")),
                                                   ("BSE bhavcopy", bhav_last.get("close"))], tol_rel=0.01)
    current_price = current[1] if (current and listed) else None
    day_chg = None
    if current and listed and current[3]:
        day_chg = pct(current[1], current[3])
    elif listed and q_nse.get("pchange") is not None:
        day_chg = q_nse.get("pchange")

    # ------------------------------------------------------------ status
    withdrawn = str(ig.get("issue_withdraw") or "0") == "1" or ndl.get("nd_section") == "CANCELLED"
    hints = [IG_STATUS.get(live.get("ig_status")), ND_SECTION.get(ndl.get("nd_section"))]
    status = compute_status(dates, hints, bool(listing_price), withdrawn, today)

    # ------------------------------------------------------------ investment by category
    app_sizes = ndd.get("app_sizes") or []
    if not app_sizes:
        app_sizes = _app_size_fallback(lot, price_high, board)
        app_src = "computed" if app_sizes else None
    else:
        app_src = "Narada"

    # ------------------------------------------------------------ reservation
    reservation = []
    for k, lab in (("shares_offered_anchor_investor", "Anchor"), ("shares_offered_qib", "QIB (ex-anchor)"),
                   ("shares_offered_nii", "NII"), ("shares_offered_big_nii", "  bNII"), ("shares_offered_small_nii", "  sNII"),
                   ("shares_offered_rii", "Retail"), ("shares_offered_emp", "Employee"),
                   ("shares_offered_shareholders", "Shareholder"), ("shares_offered_market_maker", "Market maker"),
                   ("shares_offered_others", "Others"), ("shares_offered_total", "Total")):
        v = num(ig.get(k))
        if v:
            reservation.append({"label": lab, "shares": v,
                                "pct": rnd(v / num(ig.get("shares_offered_total")) * 100, 1) if num(ig.get("shares_offered_total")) and k != "shares_offered_total" else None})

    # ------------------------------------------------------------ company info
    reg = igd.get("registrar") or {}
    reg_txt = strip_tags(reg.get("registrar_basic_info") or "")
    lead_mgrs = first(igd.get("lead_managers"), (ndd.get("intermediaries") or {}).get("Lead managers")) or []
    registrar_name = first(strip_tags(reg.get("registrar_name")), ((ndd.get("intermediaries") or {}).get("Registrar") or [None])[0])
    kpis = {}
    for k, lab in (("kpi_roe", "ROE %"), ("kpi_roce", "ROCE %"), ("kpi_ronw", "RoNW %"), ("kpi_debt_equity", "Debt/Equity"),
                   ("kpi_pat_margin", "PAT margin %"), ("kpi_ebitda", "EBITDA margin %"), ("kpi_eps", "EPS (pre) ₹"),
                   ("kpi_eps_post", "EPS (post) ₹"), ("price_to_book_value", "P/B"), ("nav", "NAV ₹")):
        v = num(ig.get(k))
        if v is not None:
            kpis[lab] = v
    fin_html = ig.get("financial") if isinstance(ig.get("financial"), str) else ""
    company = {
        "about": clean_html(first(ig.get("company_desc"), ig.get("about_company")) or ""),
        "objects": clean_html(ig.get("issue_objects") or ""),
        "promoters": strip_tags(ig.get("promoters") or "") or None,
        "financials": _fin_table(fin_html),
        "financials_html": clean_html(fin_html) if fin_html else "",
        "kpis": kpis,
        "kpi_as_of": ig.get("kpi_as_of_date"),
        "promoter_pre": num(ig.get("promoter_shareholding_pre_issue")),
        "promoter_post": num(ig.get("promoter_shareholding_post_issue")),
        "sector": first(ig.get("company_sector"), ig.get("sector")),
        "city": first(ig.get("city"), ig.get("city_name")),
        "state": ig.get("state"),
        "address": ", ".join(x for x in [ig.get("address_1"), ig.get("address_2"), ig.get("address_3"),
                                         ig.get("city"), ig.get("state")] if x) or None,
        "phone": ig.get("phone") or None, "email": ig.get("email") or None,
        "website": first(ig.get("website"), ndd.get("website")),
        "lead_managers": lead_mgrs, "registrar": registrar_name, "registrar_info": reg_txt or None,
        "allotment_url": ig.get("ipo_allotment_url") or None,
        "market_makers": (ndd.get("intermediaries") or {}).get("Market makers") or (ndd.get("intermediaries") or {}).get("Market maker"),
        "docs": {k: v for k, v in (("DRHP", ig.get("prospectus_drhp")), ("RHP", ig.get("prospectus_rhp")),
                                   ("Prospectus", ig.get("final_prospectus")), ("Anchor list", ig.get("anchor_investor_url")))
                 if v and str(v).startswith("http")},
        "anchor_html": clean_html(ig.get("anchor_investor_detail") or "") if isinstance(ig.get("anchor_investor_detail"), str) else "",
        "peer_html": clean_html(ig.get("peer_analysis") or "") if isinstance(ig.get("peer_analysis"), str) else "",
    }

    links = {}
    if slug and ig_id:
        links["InvestorGain GMP"] = f"https://www.investorgain.com/gmp/{slug}/{ig_id}/"
        links["InvestorGain subscription"] = f"https://www.investorgain.com/subscription/{slug}/{ig_id}/"
    if cg_id and slug:
        links["Chittorgarh"] = f"https://www.chittorgarh.com/ipo/{slug}/{cg_id}/"
    if nd_sym:
        links["Narada"] = f"https://trynarada.com/ipos/{nd_sym}/"
    if nse_sym:
        links["NSE"] = f"https://www.nseindia.com/get-quotes/equity?symbol={nse_sym}"
    if bse_code:
        links["BSE"] = f"https://www.bseindia.com/stock-share-price/x/{(nse_sym or 'x').lower()}/{bse_code}/"

    listing_gain = pct(listing_price, issue_price or ref_price) if listing_price else None
    listing_close_gain = pct(listing_close, issue_price or ref_price) if listing_close else None
    current_gain = pct(current_price, issue_price or ref_price) if current_price else None
    spark = [h["gmp"] for h in hist if h.get("gmp") is not None][-14:]

    verification = V.summary()
    doc = {
        "key": key, "ig_id": ig_id, "cg_id": cg_id, "nd_sym": nd_sym, "slug": slug,
        "name": name, "short": short, "board": board, "exchange": exchange,
        "nse_symbol": nse_sym, "bse_code": bse_code, "isin": ig.get("isin") or None,
        "series": ig.get("nse_listing_in_group") or nse.get("series"),
        "logo": first(ndl.get("logo"), cat.get("logo"),
                      ("https://www.chittorgarh.net/images/ipo/" + ig["logo_url"]) if ig.get("logo_url") else None),
        "status": status, "status_label": STATUS_LABEL[status], "status_order": STATUS_ORDER[status],
        "issue_type": ig.get("issue_process_type_desc") or ("Book Build Issue" if price_low != price_high else None),
        "face_value": num(ig.get("face_value")),
        "price_low": price_low, "price_high": price_high, "issue_price": issue_price,
        "lot": lot, "min_amount": rnd(lot * price_high, 0) if lot and price_high else None,
        "size_cr": size_cr, "fresh_cr": fresh_cr, "ofs_cr": ofs_cr,
        "pe": first(num(ig.get("pe_ratio")), live.get("pe")), "post_pe": num(ig.get("post_pe_ratio")),
        "market_cap_cr": num(ig.get("market_cap")),
        "rating": live.get("rating") if live.get("rating") is not None else num(ig.get("adm_rating")),
        "anchor": live.get("anchor") if live else (str(ig.get("anchor_investor_status")) == "1"),
        "dates": dates,
        "gmp": gmp, "gmp_pct": gmp_pct, "gmp_src": gmp_src, "gmp_low": gmp_low, "gmp_high": gmp_high,
        "gmp_updated": gmp_updated, "est_listing": est_listing,
        "est_profit_lot": rnd(gmp * lot, 0) if gmp is not None and lot else None,
        "gmp_ig": ig_gmp, "gmp_nd": nd_gmp, "final_gmp": perf.get("final_gmp"),
        "gmp_spark": spark,
        "sub": sub, "sub_total": sub_total, "sub_src": chosen_sub[0] if chosen_sub else None,
        "sub_as_of": (chosen_sub[1].isoformat() if chosen_sub and chosen_sub[1] else None),
        "listing_price": listing_price, "listing_gain": listing_gain,
        "listing_close": listing_close, "listing_close_gain": listing_close_gain,
        "current_price": current_price, "current_gain": current_gain, "day_change": day_chg,
        "current_src": current[0] if (current and listed) else None,
        "current_as_of": current[2] if (current and listed) else None,
        "verification": verification,
        "has_detail": bool(igd or ndd),
    }
    detail = {
        "checks": V.checks,
        "gmp_history": hist,
        "subscription": {
            "chosen_src": doc["sub_src"], "as_of": doc["sub_as_of"], "times": sub,
            "shares_nd": shares_rows, "shares_ig": ig_rows, "ig_as_of": last_bid.get("as_of") if last_bid else None,
            "apps": apps_rows, "total_applications": total_apps, "boa": boa, "boa_src": "Chittorgarh" if boa else None,
            "daywise": daywise, "summary": igs.get("summary") or [], "with_anchor": igs.get("with_anchor") or [],
            "candidates": [{"src": c[0], "as_of": c[1].isoformat() if c[1] else None, "times": c[2]} for c in sub_cands],
            "snapshots": sub_snaps[-400:] if sub_snaps else [],
            "nse_rows": nse_rows,
        },
        "app_sizes": app_sizes, "app_sizes_src": app_src,
        "reservation": reservation,
        "company": company,
        "links": links,
        "prices": {"candidates": [{"src": c[0], "price": c[1], "as_of": c[2]} for c in cur_cands if c[1]],
                   "bse_listing_day": bhav_l or None, "nse_listing_day": nbhav_l or None,
                   "nse_quote": q_nse or None, "bse_quote": q_bse or None},
        "sources": {s: (raw[s].get("_fetched") if isinstance(raw.get(s), dict) else None) for s in raw},
    }
    return doc, detail


def merge_gmp_history(rows, price):
    """rows from gmp_hist (all sources) -> one entry per day with chosen value + per-source values."""
    by_day = {}
    for r in rows or []:
        d = by_day.setdefault(r["day"], {"date": r["day"]})
        src = "ig" if r["source"] == "investorgain" else ("nd" if r["source"] == "narada" else r["source"])
        d[src] = r.get("gmp")
        if src == "ig":
            for k in ("est_listing", "sub2", "est_profit", "updated"):
                if r.get(k) not in (None, ""):
                    d[k] = r.get(k)
            if r.get("gmp_pct") is not None:
                d["ig_pct"] = r.get("gmp_pct")
    out = []
    for day in sorted(by_day):
        d = by_day[day]
        g = d.get("ig") if d.get("ig") is not None else d.get("nd")
        d["gmp"] = g
        d["gmp_pct"] = rnd(g / price * 100, 2) if (g is not None and price) else d.get("ig_pct")
        if d.get("est_listing") is None and g is not None and price:
            d["est_listing"] = rnd(price + g, 2)
        out.append(d)
    return out
