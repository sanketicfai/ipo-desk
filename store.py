"""SQLite persistence. GMP history is kept forever (sources prune theirs; we don't)."""
import json
import sqlite3
import threading

from util import iso_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS ipo (          -- merged document per IPO
  key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS raw (          -- last parsed payload per IPO per source
  key TEXT, source TEXT, data TEXT, fetched_at TEXT, PRIMARY KEY (key, source));
CREATE TABLE IF NOT EXISTS gmp_hist (     -- one row per IPO / day / source
  key TEXT, day TEXT, source TEXT, gmp REAL, gmp_pct REAL, est_listing REAL, sub2 TEXT,
  est_profit REAL, updated TEXT, PRIMARY KEY (key, day, source));
CREATE TABLE IF NOT EXISTS sub_hist (     -- subscription snapshots (live polling)
  key TEXT, ts TEXT, source TEXT, data TEXT, PRIMARY KEY (key, ts, source));
CREATE TABLE IF NOT EXISTS price_hist (   -- one close per IPO per trading day (daily series)
  key TEXT, day TEXT, close REAL, src TEXT, final INTEGER DEFAULT 0, fetched_at TEXT,
  PRIMARY KEY (key, day));
CREATE TABLE IF NOT EXISTS ohlc (          -- one daily candle per IPO per trading day (open/high/low/close/volume)
  key TEXT, day TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, src TEXT,
  PRIMARY KEY (key, day));
CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY, ok INTEGER, last_ok TEXT, last_try TEXT, last_error TEXT, calls INTEGER, fails INTEGER);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS gmp_key ON gmp_hist(key);
CREATE INDEX IF NOT EXISTS sub_key ON sub_hist(key);
CREATE INDEX IF NOT EXISTS price_key ON price_hist(key);
CREATE INDEX IF NOT EXISTS ohlc_key ON ohlc(key);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ------------------------------------------------------------------ generic
    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    # ------------------------------------------------------------------ kv
    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ------------------------------------------------------------------ raw payloads
    def put_raw(self, key, source, data):
        self.x("INSERT OR REPLACE INTO raw(key,source,data,fetched_at) VALUES(?,?,?,?)",
               (key, source, json.dumps(data, default=str), iso_now()))

    def put_raw_many(self, items):
        now = iso_now()
        self.many("INSERT OR REPLACE INTO raw(key,source,data,fetched_at) VALUES(?,?,?,?)",
                  [(k, s, json.dumps(d, default=str), now) for k, s, d in items])

    def del_raw(self, key, source):
        self.x("DELETE FROM raw WHERE key=? AND source=?", (key, source))

    def raw_for(self, key):
        out = {}
        for r in self.q("SELECT source,data,fetched_at FROM raw WHERE key=?", (key,)):
            d = json.loads(r["data"])
            if isinstance(d, dict):
                d["_fetched"] = r["fetched_at"]
            out[r["source"]] = d
        return out

    def raw_source(self, source):
        return {r["key"]: json.loads(r["data"]) for r in self.q("SELECT key,data FROM raw WHERE source=?", (source,))}

    def raw_fetched(self, source):
        return {r["key"]: r["fetched_at"] for r in self.q("SELECT key,fetched_at FROM raw WHERE source=?", (source,))}

    def move_key(self, old, new):
        """Re-key an IPO (e.g. Narada-only record later matched to an InvestorGain id)."""
        with self.lock:
            for t in ("raw", "gmp_hist", "sub_hist"):
                self.db.execute(f"UPDATE OR IGNORE {t} SET key=? WHERE key=?", (new, old))
                self.db.execute(f"DELETE FROM {t} WHERE key=?", (old,))
            self.db.execute("DELETE FROM ipo WHERE key=?", (old,))
            self.db.commit()

    # ------------------------------------------------------------------ docs
    def put_doc(self, key, doc):
        self.x("INSERT OR REPLACE INTO ipo(key,data,updated_at) VALUES(?,?,?)",
               (key, json.dumps(doc, default=str), iso_now()))

    def put_docs(self, docs):
        now = iso_now()
        self.many("INSERT OR REPLACE INTO ipo(key,data,updated_at) VALUES(?,?,?)",
                  [(k, json.dumps(d, default=str), now) for k, d in docs])

    def docs(self):
        return [json.loads(r["data"]) for r in self.q("SELECT data FROM ipo")]

    def doc(self, key):
        r = self.q("SELECT data FROM ipo WHERE key=?", (key,))
        return json.loads(r[0]["data"]) if r else None

    def keys(self):
        return [r["key"] for r in self.q("SELECT key FROM ipo")]

    # ------------------------------------------------------------------ GMP history
    def put_gmp(self, key, source, rows):
        """rows: [{date, gmp, gmp_pct, est_listing, sub2, est_profit, updated}]"""
        self.many(
            "INSERT OR REPLACE INTO gmp_hist(key,day,source,gmp,gmp_pct,est_listing,sub2,est_profit,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(key, r["date"], source, r.get("gmp"), r.get("gmp_pct"), r.get("est_listing"), r.get("sub2"),
              r.get("est_profit"), r.get("updated")) for r in rows if r.get("date")])

    def put_gmp_ignore(self, rows):
        """Insert only rows we don't already have - used when seeding history from a local database."""
        if not rows:
            return 0
        before = self.q("SELECT COUNT(*) c FROM gmp_hist")[0]["c"]
        self.many(
            "INSERT OR IGNORE INTO gmp_hist(key,day,source,gmp,gmp_pct,est_listing,sub2,est_profit,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(r.get("key"), r.get("day"), r.get("source"), r.get("gmp"), r.get("gmp_pct"),
              r.get("est_listing"), r.get("sub2"), r.get("est_profit"), r.get("updated"))
             for r in rows if r.get("key") and r.get("day")])
        return self.q("SELECT COUNT(*) c FROM gmp_hist")[0]["c"] - before

    def put_sub_ignore(self, rows):
        """Same idea for subscription snapshots."""
        if not rows:
            return 0
        before = self.q("SELECT COUNT(*) c FROM sub_hist")[0]["c"]
        self.many("INSERT OR IGNORE INTO sub_hist(key,ts,source,data) VALUES(?,?,?,?)",
                  [(r.get("key"), r.get("ts"), r.get("source"), json.dumps(r.get("data") or {}, default=str))
                   for r in rows if r.get("key") and r.get("ts")])
        return self.q("SELECT COUNT(*) c FROM sub_hist")[0]["c"] - before

    def put_ohlc_ignore(self, rows):
        """Insert only candles we don't already have - used when seeding history from a local database."""
        if not rows:
            return 0
        before = self.q("SELECT COUNT(*) c FROM ohlc")[0]["c"]
        self.many(
            "INSERT OR IGNORE INTO ohlc(key,day,open,high,low,close,volume,src) VALUES(?,?,?,?,?,?,?,?)",
            [(r.get("key"), r.get("day"), r.get("open"), r.get("high"), r.get("low"), r.get("close"),
              r.get("volume"), r.get("src") or "seed")
             for r in rows if r.get("key") and r.get("day") and r.get("close")])
        return self.q("SELECT COUNT(*) c FROM ohlc")[0]["c"] - before

    def gmp_for(self, key):
        return [dict(r) for r in self.q(
            "SELECT day,source,gmp,gmp_pct,est_listing,sub2,est_profit,updated FROM gmp_hist WHERE key=? ORDER BY day",
            (key,))]

    def gmp_all(self):
        out = {}
        for r in self.q("SELECT key,day,source,gmp,gmp_pct FROM gmp_hist ORDER BY day"):
            out.setdefault(r["key"], []).append(dict(r))
        return out

    # ------------------------------------------------------------------ subscription snapshots
    # ---------------------------------------------------------------- price history
    def put_ohlc(self, key, bars, src="yahoo"):
        """Upsert daily candles. The exchange/Yahoo value for a day replaces an older one for that day."""
        n = 0
        with self.lock:
            for b in bars or []:
                day = b.get("date") or b.get("day")
                c = b.get("close")
                if not (key and day and c):
                    continue
                self.db.execute(
                    "INSERT INTO ohlc(key, day, open, high, low, close, volume, src) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(key, day) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
                    "close=excluded.close, volume=excluded.volume, src=excluded.src WHERE excluded.close IS NOT NULL",
                    (key, day, b.get("open"), b.get("high"), b.get("low"), c, b.get("volume"), src))
                n += 1
            self.db.commit()
        return n

    def ohlc_for(self, key, limit=260):
        rows = self.q("SELECT day, open, high, low, close, volume FROM ohlc WHERE key=? ORDER BY day DESC LIMIT ?",
                      (key, limit))
        return [dict(r, date=r["day"]) for r in reversed(rows)]      # `date` for the dashboard, `day` for SQL

    def put_price(self, key, day, close, src=None, final=False):
        """Upsert one daily close. An official (final) close is never overwritten by a live tick."""
        try:
            close = float(close)
        except (TypeError, ValueError):
            return False
        if not (key and day and close > 0):
            return False
        final = 1 if final else 0
        row = self.q("SELECT close, src, final FROM price_hist WHERE key=? AND day=?", (key, day))
        if row:
            old = row[0]
            if old["final"] and not final:
                return False
            if abs((old["close"] or 0) - close) < 1e-9 and (old["src"] or "") == (src or "") and old["final"] == final:
                return False
            self.x("UPDATE price_hist SET close=?, src=?, final=?, fetched_at=? WHERE key=? AND day=?",
                   (close, src, final, iso_now(), key, day))
        else:
            self.many("INSERT INTO price_hist(key, day, close, src, final, fetched_at) VALUES(?,?,?,?,?,?)",
                      [(key, day, close, src, final, iso_now())])
        return True

    def prices_for(self, key):
        rows = self.q("SELECT day, close, src, final FROM price_hist WHERE key=? ORDER BY day", (key,))
        return [{"date": r["day"], "close": r["close"], "src": r["src"], "final": bool(r["final"])} for r in rows]

    def prices_all(self):
        out = {}
        for r in self.q("SELECT key, day, close, src, final FROM price_hist ORDER BY key, day"):
            out.setdefault(r["key"], []).append(
                {"date": r["day"], "close": r["close"], "src": r["src"], "final": bool(r["final"])})
        return out

    def put_sub(self, key, source, ts, data):
        self.x("INSERT OR REPLACE INTO sub_hist(key,ts,source,data) VALUES(?,?,?,?)",
               (key, ts, source, json.dumps(data, default=str)))

    def sub_for(self, key):
        return [{"ts": r["ts"], "source": r["source"], **json.loads(r["data"])}
                for r in self.q("SELECT ts,source,data FROM sub_hist WHERE key=? ORDER BY ts", (key,))]

    # ------------------------------------------------------------------ source health
    def mark(self, source, ok, err=None):
        now = iso_now()
        with self.lock:
            cur = self.db.execute("SELECT calls,fails,last_ok FROM source_status WHERE source=?", (source,)).fetchone()
            calls = (cur["calls"] if cur else 0) + 1
            fails = (cur["fails"] if cur else 0) + (0 if ok else 1)
            last_ok = now if ok else (cur["last_ok"] if cur else None)
            self.db.execute("INSERT OR REPLACE INTO source_status(source,ok,last_ok,last_try,last_error,calls,fails) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (source, 1 if ok else 0, last_ok, now, None if ok else str(err)[:300], calls, fails))
            self.db.commit()

    def statuses(self):
        return {r["source"]: dict(r) for r in self.q("SELECT * FROM source_status")}
