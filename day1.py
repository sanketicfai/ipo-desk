"""Day-1 dataset + day-1 strategy study.

One place builds the numbers for the screen (the "Day 1" tab), for the spreadsheet download and
for the research tab, so all three can never disagree.

Everything here is descriptive: it counts what actually happened on listing day, on the real
first-day open / high / low / close.  No forecasts, no advice.
"""
import statistics as _st


# --------------------------------------------------------------------------- helpers
def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(part, base):
    a, b = _f(part), _f(base)
    return None if (a is None or not b) else (a / b - 1.0) * 100.0


def _r(v, d=2):
    return None if v is None else round(v, d)


def _band_ok(row):
    """Does the listing day sit inside its price band?

    An SME share is held inside +-5% of the day's open for the whole first session and a
    mainboard share inside +-20%. A reported open / close / high / low that breaks out of that
    box is wrong somewhere, so it is kept out of the study instead of quietly skewing it.
    """
    l, c, h, lo = row["l"], row["c"], row["h"], row["lo"]
    if not l:
        return True
    band = 5.05 if (row["board"] or "").upper().startswith("SME") else 20.05
    if c is not None and abs(c / l - 1) * 100.0 > band:
        return False
    if h is not None and (h / l - 1) * 100.0 > band:
        return False
    if lo is not None and (1 - lo / l) * 100.0 > band:
        return False
    return True


def row(d):
    """Day-1 numbers for one IPO doc (None when the listing-day prices are not known yet)."""
    live = bool(d.get("listing_live"))                # trading today: L is the live price, no close yet
    l, c = _f(d.get("listing_price")), _f(d.get("listing_close"))
    if l is None or l <= 0 or (c is None and not live):
        return None
    h, lo = _f(d.get("listing_high")), _f(d.get("listing_low"))
    partial = h is None or lo is None
    if c is None:                                     # the session is still running: H/L are unknown
        pass
    else:
        h = h if h is not None else max(l, c)
        lo = lo if lo is not None else min(l, c)
        h, lo = max(h, l, c), min(lo, l, c)
    issue = _f(d.get("issue_price")) or _f(d.get("price_high"))
    n = _f(d.get("current_price"))
    # grey-market premium is stored in rupees over the issue price - turn it into the implied
    # GMP *price* so it can be compared with the traded price of the day
    prem, gp = _f(d.get("final_gmp")), _f(d.get("gmp_pct"))
    if prem is None:
        prem = _f(d.get("gmp"))
    gmp = None
    if issue:
        if prem is not None:
            gmp = issue + prem
        elif gp is not None:
            gmp = issue * (1 + gp / 100.0)
    if gmp is not None and gmp <= 0:
        gmp = None
    date = (d.get("dates") or {}).get("listing") or ""
    return {
        "key": d.get("key"), "keyfile": d.get("keyfile"), "name": d.get("name") or d.get("short") or "",
        "short": d.get("short") or d.get("name") or "", "symbol": d.get("nse_symbol") or d.get("bse_code") or "",
        "board": d.get("board") or "Mainboard", "exchange": d.get("exchange") or "",
        "date": date, "status": d.get("status"), "lot": _f(d.get("lot")), "size_cr": _f(d.get("size_cr")),
        "sub": _f(d.get("sub_total")), "issue": issue, "gmp": gmp,
        "l": l, "h": h, "lo": lo, "c": c, "n": n, "live": live, "volume": _f(d.get("listing_volume")),
        "value_band": d.get("value_band") or ((d.get("value") or {}) if isinstance(d.get("value"), dict) else {}).get("band"),
        "quality": {"bhavcopy": "Official exchange file", "eod": "Adjusted EOD series",
                    "indicative": "Indicative"}.get(d.get("listing_quality") or "", "Indicative"),
        "partial": partial,
        "open_gain": _r(_pct(l, issue)), "close_gain": _r(_pct(c, issue)), "high_gain": _r(_pct(h, issue)),
        "low_gain": _r(_pct(lo, issue)), "now_gain": _r(_pct(n, issue)),
        "move": _r(_pct(c, l)), "high_move": _r(_pct(h, l)), "low_move": _r(_pct(lo, l)),
        "now_move": _r(_pct(n, l)),
        "range_pct": _r((h - lo) / l * 100.0) if (h is not None and lo is not None) else None,
        "hit5": bool(h is not None and h >= l * 1.05), "hit10": bool(h is not None and h >= l * 1.10),
        "above_issue": l >= issue if issue else None,
        "above_gmp": (l >= gmp) if gmp else None,
        "close_above_gmp": (c >= gmp) if (gmp and c is not None) else None,
        "close_above_issue": (c >= issue) if (issue and c is not None) else None,
        "dropped": bool(lo is not None and lo < l * 0.95),
        "suspect": False,          # set below: reported numbers that ignore the day's price band
    }


def rows(docs):
    out = []
    for d in docs:
        try:
            r = row(d)
        except Exception:
            r = None
        if r:
            # the exchange's own file is never second-guessed; a tracker figure that breaks the
            # band is flagged and left out of the study
            r["suspect"] = (not _band_ok(r)) and r["quality"] != "Official exchange file"
            out.append(r)
    out.sort(key=lambda r: (r["date"] or "", r["name"]), reverse=True)
    return out


# --------------------------------------------------------------------------- stats
def _stats(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    if not v:
        return None
    v = sorted(v)
    q = lambda p: v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))]
    return {"n": len(v), "avg": _r(_st.mean(v)), "med": _r(_st.median(v)), "win": _r(100.0 * sum(1 for x in v if x > 0) / len(v), 1),
            "p25": _r(q(.25)), "p75": _r(q(.75)), "best": _r(v[-1]), "worst": _r(v[0]),
            "hit5": _r(100.0 * sum(1 for x in v if x >= 5) / len(v), 1), "hit10": _r(100.0 * sum(1 for x in v if x >= 10) / len(v), 1),
            "losers": _r(100.0 * sum(1 for x in v if x <= -5) / len(v), 1)}


def _theory(rows_, key, title, rule, metric="move", where=None, note="", min_n=6):
    sel = [r for r in rows_ if (where(r) if where else True) and isinstance(r.get(metric), (int, float))]
    s = _stats([r[metric] for r in sel])
    if not s:
        return {"id": key, "title": title, "rule": rule, "n": 0, "thin": True, "note": note,
                "metric": metric, "stats": None}
    s["thin"] = s["n"] < min_n
    return {"id": key, "title": title, "rule": rule, "metric": metric, "stats": s, "note": note}


def _rule_sim(rows_, target=5.0, stop=7.0):
    """Target / stop / 3:30-close exit rule, from the real open-high-low-close of listing day.

    Conservative read: if the day's low touched the stop we assume the stop filled first, then the
    target, otherwise the position is closed at the 3:30 close.  Returns (list of returns, counters).
    """
    out, tgt, stp, close = [], 0, 0, 0
    for r in rows_:
        l, h, lo, c = r["l"], r["h"], r["lo"], r["c"]
        if not l or c is None:
            continue
        if lo <= l * (1 - stop / 100.0):
            out.append(-stop); stp += 1
        elif h >= l * (1 + target / 100.0):
            out.append(target); tgt += 1
        else:
            out.append((c / l - 1) * 100.0); close += 1
    return out, {"target": tgt, "stop": stp, "close": close}


def study(rows_, min_n=6):
    """All the day-1 questions the data can answer, as a list of comparison-ready blocks."""
    clean = [x for x in rows_ if not x.get("suspect")]
    dropped = len(rows_) - len(clean)
    r = clean
    blocks = []

    # -------- how the listing day itself behaves ------------------------------------
    blocks.append({"group": "How much does a listing day move?", "items": [
        _theory(r, "open_to_close", "Buy at the 10 a.m. open, sell at the 3:30 close",
                "Every IPO with listing-day prices — the plain day-1 trade", metric="move",
                note="This is the baseline anyone buying on listing day actually gets."),
        _theory(r, "best_exit", "Buy at the open, sell at the day's high",
                "Upper edge — the best exit that existed on listing day", metric="high_move",
                note="Not achievable in practice; it shows the ceiling, so a target can be placed."),
        _theory(r, "worst_exit", "Buy at the open, sell at the day's low",
                "Lower edge — the worst moment of the listing day", metric="low_move",
                note="The drawdown a buyer at 10 a.m. had to sit through."),
        _theory(r, "hold_now", "Buy at the open, still holding today",
                "Listing-day buyer who never sold", metric="now_move",
                note="Compares 'book the day-1 move' against 'hold the listing-day entry'."),
    ]})

    touch = {"n": len(r), "avg": _r(100.0 * sum(1 for x in r if x["hit5"]) / max(1, len(r)), 1),
             "win": _r(100.0 * sum(1 for x in r if x["hit5"]) / max(1, len(r)), 1),
             "med": _r(100.0 * sum(1 for x in r if x["hit10"]) / max(1, len(r)), 1),
             "hit5": _r(100.0 * sum(1 for x in r if x["hit5"]) / max(1, len(r)), 1),
             "hit10": _r(100.0 * sum(1 for x in r if x["hit10"]) / max(1, len(r)), 1),
             "p25": None, "p75": None, "best": None, "worst": None,
             "losers": _r(100.0 * sum(1 for x in r if x["dropped"]) / max(1, len(r)), 1), "thin": len(r) < min_n}
    blocks.append({"group": "How often is a 5-10% day-1 move actually available?", "items": [
        {"id": "touch5", "title": "Day high reached +5% over the open",
         "rule": "A +5% limit sell from the 10 a.m. price would have filled", "metric": "move",
         "stats": touch,
         "note": "+5% shown as the headline number, +10% in the median column, "
                 "and the share whose low went 5% below the open in the worst column."},
    ]})

    # -------- the user's hypothesis -------------------------------------------------
    A, B = True, False      # `is True` / `is False` keeps the "no GMP read" rows out of both sides
    blocks.append({"group": "Listed above GMP and closed above GMP (the 'above GMP' theory)", "items": [
        _theory(r, "gmp_above_both", "Opened above GMP and closed above GMP",
                "Listing-day open >= GMP and the 3:30 close >= GMP", metric="move",
                where=lambda x: x["above_gmp"] is A and x["close_above_gmp"] is A,
                note="The case to check first: a strong start that held all day."),
        _theory(r, "gmp_above_open_only", "Opened above GMP, but closed below GMP",
                "Open >= GMP, close < GMP", metric="move",
                where=lambda x: x["above_gmp"] is A and x["close_above_gmp"] is B,
                note="A start that faded - the opposite side of the same test."),
        _theory(r, "gmp_below_both", "Opened and closed below GMP",
                "Open < GMP and close < GMP", metric="move",
                where=lambda x: x["above_gmp"] is B and x["close_above_gmp"] is B,
                note="Discount to GMP on both ends."),
        _theory(r, "gmp_close_only", "Opened below GMP, closed above GMP",
                "Open < GMP, close >= GMP", metric="move",
                where=lambda x: x["above_gmp"] is B and x["close_above_gmp"] is A,
                note="Recovered through GMP during the session."),
        _theory(r, "gmp_any", "Any IPO that had a GMP at all",
                "Same trade on every listing where a GMP was quoted", metric="move",
                where=lambda x: x["gmp"] is not None,
                note="Reference sample so the four cases above can be compared against it."),
    ]})

    # -------- the same ideas, but using only what is known at 10 a.m. ----------------
    blocks.append({"group": "The signal you actually have at 10 a.m. (no hindsight)", "items": [
        _theory(r, "open_above_gmp", "Opened above its GMP price",
                "Buy at the 10 a.m. open whenever the opening price was at or above the GMP-implied price",
                metric="move", where=lambda x: x["above_gmp"] is True,
                note="This one is knowable while the share trades - the close is not."),
        _theory(r, "open_below_gmp", "Opened below its GMP price",
                "Same trade on the other side of the GMP line", metric="move",
                where=lambda x: x["above_gmp"] is False,
                note="Compare with the line above: the GMP line separates the two halves."),
        _theory(r, "open_above_issue", "Opened at or above the issue price",
                "Buy the 10 a.m. open, sell at 3:30, only when the share listed flat or higher", metric="move",
                where=lambda x: x["above_issue"] is True),
        _theory(r, "open_below_issue", "Opened below the issue price",
                "Same trade when the share listed below its issue price", metric="move",
                where=lambda x: x["above_issue"] is False,
                note="Discount listings behave differently - usually the weaker half."),
    ]})

    # -------- opening-gain buckets --------------------------------------------------
    buckets = [("start_neg", "Opened below the issue price", lambda x: x["open_gain"] is not None and x["open_gain"] < 0),
               ("start_0_10", "Opened 0-10% above issue", lambda x: x["open_gain"] is not None and 0 <= x["open_gain"] < 10),
               ("start_10_30", "Opened 10-30% above issue", lambda x: x["open_gain"] is not None and 10 <= x["open_gain"] < 30),
               ("start_30_60", "Opened 30-60% above issue", lambda x: x["open_gain"] is not None and 30 <= x["open_gain"] < 60),
               ("start_60", "Opened 60%+ above issue", lambda x: x["open_gain"] is not None and x["open_gain"] >= 60)]
    blocks.append({"group": "Does the size of the opening pop matter?", "items": [
        _theory(r, k, t, "10 a.m. open to 3:30 close, grouped by how the share opened vs the issue price",
                where=w, metric="move") for k, t, w in buckets]})

    # -------- subscription ----------------------------------------------------------
    subs = [("sub_lt10", "Subscribed under 10x", lambda x: x["sub"] is not None and x["sub"] < 10),
            ("sub_10_50", "Subscribed 10-50x", lambda x: x["sub"] is not None and 10 <= x["sub"] < 50),
            ("sub_50_150", "Subscribed 50-150x", lambda x: x["sub"] is not None and 50 <= x["sub"] < 150),
            ("sub_150", "Subscribed 150x+", lambda x: x["sub"] is not None and x["sub"] >= 150)]
    blocks.append({"group": "Does the subscription frenzy predict the day-1 trade?", "items": [
        _theory(r, k, t, "10 a.m. open to 3:30 close, grouped by oversubscription",
                where=w, metric="move") for k, t, w in subs]})

    # -------- GMP size --------------------------------------------------------------
    gmps = [("gmp_neg", "GMP was negative", lambda x: x["gmp"] is not None and x["gmp"] <= x["issue"] if x["issue"] else False),
            ("gmp_pos", "GMP was positive", lambda x: x["gmp"] is not None and x["issue"] is not None and x["gmp"] > x["issue"])]
    blocks.append({"group": "Does the grey-market premium predict the day-1 trade?", "items": [
        _theory(r, k, t, "10 a.m. open to 3:30 close, grouped by the GMP read just before listing",
                where=w, metric="move") for k, t, w in gmps]})

    # -------- board / size ----------------------------------------------------------
    blocks.append({"group": "Board and issue size", "items": [
        _theory(r, "board_sme", "SME issues", "10 a.m. open to 3:30 close, SME platform",
                where=lambda x: (x["board"] or "").upper().startswith("SME"), metric="move"),
        _theory(r, "board_main", "Mainboard issues", "10 a.m. open to 3:30 close, mainboard",
                where=lambda x: not (x["board"] or "").upper().startswith("SME"), metric="move"),
        _theory(r, "size_small", "Small issues (under ₹100 Cr)", "10 a.m. open to 3:30 close",
                where=lambda x: x["size_cr"] is not None and x["size_cr"] < 100, metric="move"),
        _theory(r, "size_big", "Large issues (₹100 Cr and above)", "10 a.m. open to 3:30 close",
                where=lambda x: x["size_cr"] is not None and x["size_cr"] >= 100, metric="move"),
    ]})

    # -------- what the day's price band allows ---------------------------------------
    def _cap(board_key, edge, label):
        sub = [x for x in r if x["move"] is not None
               and ((x["board"] or "").upper().startswith("SME") == (board_key == "sme"))
               and abs(x["move"] - edge) < 0.06]
        tot = sum(1 for x in r if x["move"] is not None
                  and ((x["board"] or "").upper().startswith("SME") == (board_key == "sme")))
        return {"id": f"band_{board_key}_{label}", "title": f"{label}", "rule": "", "metric": None, "note": "",
                "stats": {"n": len(sub), "avg": round(100.0 * len(sub) / tot, 1) if tot else None,
                          "med": None, "win": None, "p25": None, "p75": None, "best": None, "worst": None,
                          "hit5": None, "hit10": None, "losers": None, "thin": False}}

    sme_moves = [x["move"] for x in r if x["move"] is not None and (x["board"] or "").upper().startswith("SME")]
    main_moves = [x["move"] for x in r if x["move"] is not None and not (x["board"] or "").upper().startswith("SME")]
    blocks.append({"group": "What the day's price band allows", "items": [
        {"id": "band_sme", "title": "SME: the whole session is capped at +5% and -5% from the open",
         "rule": "Day-1 price band on the SME platform", "metric": None, "note": "", "stats": {
             "n": len(sme_moves), "avg": None, "med": round(_st.median(sme_moves), 2) if sme_moves else None,
             "win": None, "p25": None, "p75": None, "best": round(max(sme_moves), 2) if sme_moves else None,
             "worst": round(min(sme_moves), 2) if sme_moves else None, "hit5": None, "hit10": None,
             "losers": None, "thin": False}},
        _cap("sme", 5.0, "SME names that finished pinned at the +5% upper end"),
        _cap("sme", -5.0, "SME names that finished pinned at the -5% lower end"),
        {"id": "band_main", "title": "Mainboard: the session swings inside a +-20% band",
         "rule": "Day-1 price band on the mainboard", "metric": None, "note": "", "stats": {
             "n": len(main_moves), "avg": None, "med": round(_st.median(main_moves), 2) if main_moves else None,
             "win": None, "p25": None, "p75": None, "best": round(max(main_moves), 2) if main_moves else None,
             "worst": round(min(main_moves), 2) if main_moves else None, "hit5": None, "hit10": None,
             "losers": None, "thin": False}},
        _cap("main", 20.0, "Mainboard names that finished at the +20% upper end"),
    ], "note": "This is the single most important fact for any 10 a.m. to 3:30 plan: on an SME share "
               "+5% is the mathematical best case, while mainboard names can run to +20%."})

    # -------- rule tests -------------------------------------------------------------
    rets, cnt = _rule_sim(r, target=5.0, stop=7.0)
    sim = _stats(rets)
    rule_items = [{"id": "rule_5_7", "title": "Target +5%, stop -7%, else exit at 3:30 close",
                   "rule": "Applied to every listing day in the sample", "metric": "move",
                   "stats": ({**sim, "thin": len(rets) < min_n} if sim else None),
                   "note": f"{cnt['target']} hit the target, {cnt['stop']} hit the stop, "
                           f"{cnt['close']} were carried to the close."}]
    for tgt in (3.0, 10.0, 15.0):
        r2, c2 = _rule_sim(r, target=tgt, stop=7.0)
        s2 = _stats(r2)
        rule_items.append({"id": f"rule_{int(tgt)}_7", "title": f"Target +{int(tgt)}%, stop -7%",
                           "rule": "Same rule with a different target", "metric": "move",
                           "stats": ({**s2, "thin": len(r2) < min_n} if s2 else None),
                           "note": f"{c2['target']} hit +{int(tgt)}%, {c2['stop']} hit -7%, {c2['close']} closed at 3:30."})
    blocks.append({"group": "Trading rules tested on real listing days", "items": rule_items,
                   "note": "Only the day's open/high/low/close are used. If both the target and the stop were "
                           "touched inside the same day the stop is assumed to fill first, so these returns are "
                           "the pessimistic reading, not the flattering one."})

    # -------- quality / coverage -----------------------------------------------------
    off = sum(1 for x in r if x["quality"] == "Official exchange file")
    eod = sum(1 for x in r if x["quality"] == "Adjusted EOD series")
    ind = len(r) - off - eod
    sme = sum(1 for x in r if (x["board"] or "").upper().startswith("SME"))
    blocks.append({"group": "Data coverage behind these numbers", "items": [
        {"id": "cov_all", "title": "Listings with real listing-day prices", "rule": "The whole sample",
         "metric": None, "note": "", "stats": {"n": len(r), "avg": None, "med": None, "win": None, "p25": None,
                                               "p75": None, "best": None, "worst": None, "hit5": None, "hit10": None,
                                               "losers": None, "thin": False}},
        {"id": "cov_off", "title": "From the official exchange file", "rule": "Highest accuracy", "metric": None,
         "note": "", "stats": {"n": off, "thin": False}},
        {"id": "cov_eod", "title": "From the exchange end-of-day history", "rule": "Same exchange, EOD feed",
         "metric": None, "note": "", "stats": {"n": eod, "thin": False}},
        {"id": "cov_ind", "title": "Tracker-reported (indicative)", "rule": "Lowest accuracy",
         "metric": None, "note": "", "stats": {"n": ind, "thin": False}},
        {"id": "cov_sme", "title": "SME listings in the sample", "rule": "SME board", "metric": None,
         "note": "", "stats": {"n": sme, "thin": False}},
        {"id": "cov_band", "title": "Left out: figures that break the day's price band", "rule": "Tracker data only",
         "metric": None, "note": "", "stats": {"n": dropped, "thin": False}},
    ]})

    return {"rows": len(r), "dropped": dropped, "blocks": blocks,
            "notes": ["Returns are percentages of the listing-day open (the 10 a.m. price).",
                      "SME shares trade inside a +-5% band on listing day; mainboard inside +-20% - that is the ceiling on any day-1 plan.",
                      "Only day-1 open / high / low / close are used - no estimates, no intraday guesswork.",
                      "Small samples (fewer than %d listings) are flagged THIN." % min_n]}


# --------------------------------------------------------------------------- export
SHEET_COLS = [
    ("name", "Company", "text"), ("board", "Board", "text"), ("date", "Listing date", "text"),
    ("issue", "Issue price", "money"), ("l", "Day-1 open 10:00", "money"), ("h", "Day-1 high", "money"),
    ("lo", "Day-1 low", "money"), ("c", "Day-1 close 15:30", "money"), ("volume", "Day-1 volume", "int"),
    ("open_gain", "Open vs issue %", "pct"), ("close_gain", "Close vs issue %", "pct"),
    ("move", "Day-1 move open->close %", "pct"), ("high_move", "Best case, open to high %", "pct"),
    ("low_move", "Worst case, open to low %", "pct"), ("range_pct", "Day-1 range %", "pct"),
    ("hit5", "+5% touched", "yn"), ("hit10", "+10% touched", "yn"),
    ("gmp", "GMP before listing", "money"), ("above_gmp", "Opened above GMP", "yn"),
    ("close_above_gmp", "Closed above GMP", "yn"), ("n", "Price now", "money"),
    ("now_gain", "Now vs issue %", "pct"), ("quality", "Data quality", "text"),
]


def _cell_val(r, key, kind):
    v = r.get(key)
    if v is None:
        return ""
    if kind == "yn":
        return "yes" if v else "no"
    if kind == "money":
        return round(float(v), 2)
    if kind == "int":
        return int(v)
    return v


def sheet_rows(rows_):
    head = [c[1] for c in SHEET_COLS]
    body = [[_cell_val(r, k, t) for k, _, t in SHEET_COLS] for r in rows_]
    return [head] + body


def sheet_widths():
    return [30] + [13] * (len(SHEET_COLS) - 2) + [22]


def study_rows(study_):
    head = ["Group", "Test", "Rule", "n", "Average %", "Median %", "Win rate %", "Best %", "Worst %",
            "+5% rate", "+10% rate", "-5% rate", "Note"]
    body = []
    for b in study_["blocks"]:
        for it in b["items"]:
            s = it.get("stats") or {}
            body.append([b["group"], it["title"], it.get("rule") or "", s.get("n"),
                         s.get("avg"), s.get("med"), s.get("win"), s.get("best"), s.get("worst"),
                         s.get("hit5"), s.get("hit10"), s.get("losers"), it.get("note") or ""])
    return [head] + body


def payload(docs, generated=""):
    rows_ = rows(docs)
    st = study(rows_)
    return {"generated": generated, "count": len(rows_), "rows": rows_, "study": st}


def export(docs, out_dir, generated="", csv_path=None, xlsx_path=None):
    """Write day1.csv + day1.xlsx and return the payload the tab and the API both use."""
    import csv as _csv
    import os
    import xlsx as _xlsx
    p = payload(docs, generated)
    cols = [c[1] for c in SHEET_COLS]
    keys = [c[0] for c in SHEET_COLS]
    with open(csv_path or os.path.join(out_dir, "day1.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        for r in p["rows"]:
            w.writerow([_cell_val(r, k, t) for k, _, t in SHEET_COLS])
    readme = [["Day-1 data - how to read it"], [""],
              ["Every row is one share that has already listed. L is the first-day open (the price when the"],
               ["share started trading at 10:00), C is its first-day close at 15:30, N is the price now."],
               [""],
              ["Day-1 move" , "close / open - 1. This is what a buy at the 10 a.m. open and a sell at 3:30 returned."],
              ["Best case", "the day's high / open - 1: the ceiling that existed, not a trade you could plan."],
              ["Worst case", "the day's low / open - 1: the drawdown a 10 a.m. buyer sat through."],
              ["Touched +5% / +10%", "the day's high reached that level above the open, so a limit sell would have filled."],
              ["GMP before listing", "the grey-market premium quoted just before listing - unofficial and indicative."],
              ["Data quality", "official exchange file = full market file; exchange EOD history = the same exchange feed;"],
               ["", "indicative = tracker-reported. Rows with indicative data are marked and should be treated as an estimate."],
               [""],
              ["Study sheet", "the same returns split by opening price, subscription, board, size and GMP, plus target/stop rules."],
              [""],
              ["This is a personal research file, not investment advice. I am not SEBI registered. Invest at your own risk."]]
    _xlsx.save(xlsx_path or os.path.join(out_dir, "day1.xlsx"), [
        {"name": "Day 1 data", "rows": sheet_rows(p["rows"]), "widths": sheet_widths()},
        {"name": "Day-1 study", "rows": study_rows(p["study"]), "widths": [34, 46, 44, 7, 10, 10, 10, 9, 9, 9, 9, 9, 60]},
        {"name": "Read me", "rows": readme, "widths": [22, 110], "autofilter": False, "freeze": False},
    ])
    return p
