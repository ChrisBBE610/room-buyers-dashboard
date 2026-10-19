"""Room buyers -> BGS / Business Icons: live dashboard. Python stdlib only.

Run:  python server.py        then open http://localhost:8767

Question it answers: of the people who bought in the room at an in-person preview event
(from 14 Oct 2026 on), who has gone on to book a Business Growth Secrets date, a Business Icons
date, or both?

Rules
  events    in-person preview days that start on or after START: Social Media Monetisation (SMMU),
            Business Scale Up (HQ Chelmsford and other towns) and Business Start Up days.
            Webinars (Scale Up Online, Encore webinars) are left out, and so are BGS / Icons /
            Mastermind / Gold Circle / implementation days themselves.
  room sale a confirmed order (state 'sale') on the event's Sales Orders tab (event.event.sale_ids),
            dated on or after the event day, at least SALE_MIN ex VAT. That leaves out the £27 VIP
            tickets and VIP upgrades. Test people are left out.
  buyer     one person (matched by Odoo partner, else email). Someone who bought at two events is
            credited to the first.
  booked    a registration that isn't cancelled on a BGS event or a Business Icons event (in person,
            not the Encore webinar) that takes place on or after the day they bought. Matched on the
            buyer's partner or email.

Configuration (environment variables, for hosting): ODOO_URL, ODOO_DB, ODOO_LOGIN, ODOO_API_KEY,
DASHBOARD_USER / DASHBOARD_PASSWORD (basic auth off localhost) or DASHBOARD_PUBLIC=true, PORT.
Locally the Odoo settings fall back to the odoo MCP entry in the Claude desktop config.
"""
import base64
import hmac
import json
import os
import re
import sys
import threading
import time
import xmlrpc.client
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8767))
HOST = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("BIND_PUBLIC") else "127.0.0.1"
HERE = os.path.dirname(os.path.abspath(__file__))
ODOO_LOGIN = os.environ.get("ODOO_LOGIN", "chris.cook@bigbusinessentrepreneurs.com")
DASH_USER = os.environ.get("DASHBOARD_USER", "bbe")
DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DASH_PUBLIC = os.environ.get("DASHBOARD_PUBLIC", "").lower() in ("1", "true", "yes")

START = date(2026, 10, 14)
SALE_MIN = 50
REFRESH = 300


def etype(name):
    """The preview-event family, or None for anything that isn't an in-person preview day."""
    n = (name or "").strip()
    if n.startswith("Social Media Monetisation"):
        return "SMMU"
    if n in ("Business Scale Up Breakthrough Day", "Business Scale Up Day"):
        return "BSU HQ"
    if n.startswith("Business Scale Up -") and "Gold Circle" not in n and "A Big Business" not in n:
        return "BSU"
    if n.startswith("Business Start Up"):
        return "Start Up"
    return None


def destination(ev):
    """'BGS', 'Icons' or None for an Odoo event."""
    n = (ev["name"] or "").lower()
    t = (ev["event_type_id"] or [0, ""])[1].lower()
    if "growth secrets" in n or "growth secrets" in t:
        return "BGS"
    if "business icons" in n and "webinar" not in t and "encore" not in n:
        return "Icons"
    return None


# ---------------------------------------------------------------- time

def _last_sunday(y, m):
    d = (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def london(d):
    d = d.astimezone(timezone.utc)
    s = datetime.combine(_last_sunday(d.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    e = datetime.combine(_last_sunday(d.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return (d + timedelta(hours=1 if s <= d < e else 0)).replace(tzinfo=None)


def utc(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local_day(s):
    return london(utc(s)).date()


def today():
    return london(datetime.now(timezone.utc)).date()


# ---------------------------------------------------------------- Odoo

def odoo_conf():
    keys = ("ODOO_URL", "ODOO_DB", "ODOO_API_KEY")
    if all(os.environ.get(k) for k in keys):
        return {k: os.environ[k] for k in keys}
    path = os.path.expandvars(r"%APPDATA%/Claude/claude_desktop_config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["mcpServers"]["odoo"]["env"]


class Odoo:
    def __init__(self):
        e = odoo_conf()
        self.url, self.db, self.key = e["ODOO_URL"], e["ODOO_DB"], e["ODOO_API_KEY"]
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(self.db, ODOO_LOGIN, self.key, {})
        if not self.uid:
            raise RuntimeError("Odoo login failed - check ODOO_LOGIN / ODOO_API_KEY")

    def call(self, model, method, *args, **kw):
        obj = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return obj.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)


def is_test(email, name=""):
    """Test sign-ups (ads agency, staff tests, Chris Cook) - same rule as the events ads dashboard."""
    e, n = (email or "").strip().lower(), (name or "").strip().lower()
    local, _, domain = e.partition("@")
    label = domain.split(".")[0]
    return bool(
        "clickstoconversions" in domain or re.match(r"(test|wahid)?ctc", label)
        or domain.endswith((".test", ".ctc", ".bbe")) or domain == "bbe.com"
        or (domain.endswith(".in") and "test" in domain)
        or label in ("test", "test123", "christest", "testevents") or local.startswith("chris.cook")
        or local == "wahid" or local == "test" or re.match(r"test([^a-z]|v\d|wahid|ctc|bbe|resp)", local)
        or ("test" in local and re.search(r"wahid|ctc|control|chris|maria|sahil|ilsa", local))
        or re.search(r"\btest(\b|\d|v\d)", n) or ("chris" in n and "cook" in n))


def person_name(partner_name):
    """'Company, Person' -> 'Person' (Odoo shows contacts of a company that way)."""
    return partner_name.split(", ", 1)[1] if ", " in partner_name else partner_name


def short_event(name, city):
    t = etype(name)
    town = city or (name.split(" - ", 1)[1] if " - " in name else "")
    town = re.sub(r"\s*\*.*$|\s*\(copy\)", "", town).strip()      # drop "* TBC - NEED TO SORT ..." notes
    if t == "BSU HQ":
        return "BSU · HQ Chelmsford"
    if t == "Start Up":
        return f"Start Up · {town or 'Chelmsford'}"
    return f"{t} · {town}".strip(" ·")


def tidy(text):
    return re.sub(r"\s+", " ", (text or "").replace("Monitisation", "Monetisation")).strip()


def build():
    od = Odoo()
    t = today()
    since = (datetime.combine(START, datetime.min.time()) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    evs = od.call("event.event", "search_read", [["date_begin", ">=", since]],
                  fields=["name", "date_begin", "stage_id", "sale_ids", "address_id", "event_type_id"],
                  context={"active_test": False})
    addr_ids = list({e["address_id"][0] for e in evs if e["address_id"]})
    city = {a["id"]: a["city"] or "" for a in od.call("res.partner", "read", addr_ids, fields=["city"])} if addr_ids else {}

    previews = []
    for e in evs:
        if not etype(e["name"]) or local_day(e["date_begin"]) < START:
            continue
        if "cancel" in (e["stage_id"] or [0, ""])[1].lower() and not e["sale_ids"]:
            continue
        previews.append(e)
    previews.sort(key=lambda e: e["date_begin"])

    order_ids = [i for e in previews for i in e["sale_ids"]]
    orders = od.call("sale.order", "read", order_ids,
                     fields=["name", "partner_id", "date_order", "amount_untaxed", "state",
                             "bbe_dropdown_product_names", "bbe_payment_status"]) if order_ids else []
    by_id = {o["id"]: o for o in orders}
    pids = list({o["partner_id"][0] for o in orders})
    partners = {p["id"]: p for p in od.call("res.partner", "read", pids, fields=["email", "name"])} if pids else {}

    # room sales -> buyers (first room purchase wins)
    buyers, events_out = {}, []
    for e in previews:
        day = local_day(e["date_begin"])
        label = short_event(e["name"], city.get((e["address_id"] or [0])[0], ""))
        sales = 0
        for oid in e["sale_ids"]:
            o = by_id.get(oid)
            if not o or o["state"] != "sale" or o["amount_untaxed"] < SALE_MIN or local_day(o["date_order"]) < day:
                continue
            p = partners.get(o["partner_id"][0], {})
            email = (p.get("email") or "").strip().lower()
            if is_test(email, o["partner_id"][1]):
                continue
            sales += 1
            key = email or f"p{o['partner_id'][0]}"
            b = buyers.get(key)
            if b is None:
                b = buyers[key] = {"name": tidy(person_name(o["partner_id"][1])), "email": email,
                                   "partners": set(), "eventId": e["id"], "event": label,
                                   "eventDay": day.isoformat(), "boughtDay": local_day(o["date_order"]).isoformat(),
                                   "orders": [], "total": 0.0, "bgs": [], "icons": []}
            b["partners"].add(o["partner_id"][0])
            b["orders"].append({"ref": o["name"], "product": tidy(o["bbe_dropdown_product_names"]),
                                "amount": o["amount_untaxed"], "paid": o["bbe_payment_status"] or "",
                                "day": local_day(o["date_order"]).isoformat(), "event": label})
            b["total"] += o["amount_untaxed"]
        cancelled = "cancel" in (e["stage_id"] or [0, ""])[1].lower()
        if cancelled and not sales:
            continue
        events_out.append({"id": e["id"], "label": label, "day": day.isoformat(),
                           "past": day < t, "sales": sales})

    # bookings on BGS / Icons events
    dest_evs = od.call("event.event", "search_read",
                       ["|", "|", ["name", "ilike", "growth secrets"], ["name", "ilike", "business icons"],
                        ["event_type_id.name", "ilike", "growth secrets"]],
                       fields=["name", "date_begin", "event_type_id", "stage_id"], context={"active_test": False})
    dest = {e["id"]: (destination(e), e) for e in dest_evs if destination(e)}
    all_pids = list({p for b in buyers.values() for p in b["partners"]})
    emails = [b["email"] for b in buyers.values() if b["email"]]
    regs = []
    if dest and (all_pids or emails):
        dom = [["event_id", "in", list(dest)], ["state", "!=", "cancel"]]
        dom += ["|", ["partner_id", "in", all_pids], ["email", "in", emails]] if emails else [["partner_id", "in", all_pids]]
        regs = od.call("event.registration", "search_read", dom,
                       fields=["partner_id", "email", "event_id", "create_date", "state"], context={"active_test": False})
    by_pid, by_email = {}, {}
    for key, b in buyers.items():
        for p in b["partners"]:
            by_pid[p] = key
        if b["email"]:
            by_email[b["email"]] = key
    for r in regs:
        key = by_pid.get((r["partner_id"] or [0])[0]) or by_email.get((r["email"] or "").strip().lower())
        if not key:
            continue
        b = buyers[key]
        kind, ev = dest[r["event_id"][0]]
        ev_day = local_day(ev["date_begin"])
        if ev_day < date.fromisoformat(b["boughtDay"]):
            continue
        lst = b["bgs" if kind == "BGS" else "icons"]
        if any(x["eventId"] == ev["id"] for x in lst):
            continue
        lst.append({"eventId": ev["id"], "date": ev_day.isoformat(), "bookedOn": local_day(r["create_date"]).isoformat(),
                    "attended": r["state"] == "done"})

    out = []
    for b in buyers.values():
        b["bgs"].sort(key=lambda x: x["date"])
        b["icons"].sort(key=lambda x: x["date"])
        b["status"] = ("both" if b["bgs"] and b["icons"] else "bgs" if b["bgs"] else "icons" if b["icons"] else "none")
        b.pop("partners")
        b.pop("email")
        out.append(b)
    out.sort(key=lambda b: (b["eventDay"], b["name"].lower()))
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "today": t.isoformat(), "start": START.isoformat(),
            "saleMin": SALE_MIN, "events": events_out, "buyers": out}


# ---------------------------------------------------------------- serving

class Cache:
    def __init__(self):
        self.data, self.error, self.lock = None, None, threading.Lock()

    def refresh(self):
        try:
            d = build()
            with self.lock:
                self.data, self.error = d, None
        except Exception as ex:          # keep the last good data on screen
            with self.lock:
                self.error = f"{type(ex).__name__}: {ex}"
            print("refresh failed:", self.error, flush=True)

    def loop(self):
        while True:
            self.refresh()
            time.sleep(REFRESH)

    def payload(self):
        with self.lock:
            return {**(self.data or {"buyers": [], "events": []}), "error": self.error, "loading": self.data is None}


CACHE = Cache()


class Handler(BaseHTTPRequestHandler):
    def authorised(self):
        if DASH_PUBLIC or not DASH_PASSWORD:
            return True
        expected = "Basic " + base64.b64encode(f"{DASH_USER}:{DASH_PASSWORD}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if not self.authorised():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Room buyers dashboard"')
            self.end_headers()
            return
        if self.path.startswith("/api/data"):
            if "force=1" in self.path:
                CACHE.refresh()
            body, ctype = json.dumps(CACHE.payload()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(json.dumps(build(), indent=1))
        sys.exit()
    if HOST != "127.0.0.1" and not DASH_PASSWORD and not DASH_PUBLIC:
        sys.exit("Refusing to start on a public address without DASHBOARD_PASSWORD (or DASHBOARD_PUBLIC=true).")
    threading.Thread(target=CACHE.loop, daemon=True).start()
    print(f"Room buyers dashboard on http://{HOST}:{PORT}  (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
