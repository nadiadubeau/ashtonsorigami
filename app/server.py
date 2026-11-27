#!/usr/bin/env python3
"""Ashton's Origami - order app server.

Standard library only (Python 3.8+). Layout on the server:
  ROOT/run.py            launcher (restarts the app, rolls back a broken update)
  ROOT/current ->        symlink to the version in use, e.g. ROOT/versions/1.0.0
  ROOT/versions/<v>/     server.py, index.html, VERSION
  ROOT/data/             store.json, users.json, config.json, state.json (never touched by updates)
"""
import base64, csv, hashlib, hmac, io, json, os, py_compile, re, secrets, shutil, sys, tempfile
import threading, time, urllib.error, urllib.parse, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AO_ROOT", "/opt/ashtonsorigami")
DATA = os.path.join(ROOT, "data")
VERS = os.path.join(ROOT, "versions")
CURRENT = os.path.join(ROOT, "current")
PORT = int(os.environ.get("AO_PORT", "8091"))
HOST = os.environ.get("AO_HOST", "127.0.0.1")
VERSION = open(os.path.join(APP_DIR, "VERSION")).read().strip()
COLLS = ("orders", "products", "notes", "leads", "costs")
DOCS = ("settings", "colours")
KEEP_VERSIONS = 10
SESSION_DAYS = 30
LOCK = threading.RLock()
os.makedirs(DATA, exist_ok=True)


# ---------------------------------------------------------------- storage
def jpath(name):
    return os.path.join(DATA, name)


def jload(name, default):
    try:
        with open(jpath(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def jsave(name, obj):
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix="." + name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, jpath(name))


def default_store():
    return {
        "rev": 1, "resetRev": 1, "nextOrder": 1, "tomb": [],
        "records": {c: {} for c in COLLS},
        "docs": {
            "settings": {"rev": 1, "data": {"name": "Ashton's Origami", "claude": "", "returnAddress": "",
                                            "rates": [{"id": "canada", "name": "Within Canada", "price": 3}]}},
            "colours": {"rev": 1, "data": [{"id": "c%d" % i, "name": n, "hex": h} for i, (n, h) in enumerate([
                ("Red", "#d62f2a"), ("Orange", "#f28c28"), ("Yellow", "#f6d13a"), ("Green", "#3aa655"),
                ("Teal", "#2a9d8f"), ("Light blue", "#6cb4e8"), ("Navy", "#22366b"), ("Purple", "#8a63b8"),
                ("Pink", "#f49ac1"), ("White", "#ffffff"), ("Black", "#222222"), ("Gold", "#d4af37"),
                ("Silver", "#c0c0c0")])]},
        },
    }


STORE = jload("store.json", None) or default_store()
for _c in COLLS:
    STORE["records"].setdefault(_c, {})
USERS = jload("users.json", {})          # username -> {name, role, salt, hash}
SESSIONS = jload("sessions.json", {})    # token -> {user, exp}
CONFIG = jload("config.json", {})        # secrets: github, dropbox
STATE = jload("state.json", {})          # versions, update info, sync status
DIRTY = {"dropbox": False, "notes": False}
FAILS = {}


def save_store():
    jsave("store.json", STORE)
    DIRTY["dropbox"] = True


def bump():
    STORE["rev"] += 1
    return STORE["rev"]


# ---------------------------------------------------------------- passwords
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240000).hex()


def check_pw(user, pw):
    u = USERS.get(user)
    if not u:
        hash_pw(pw)  # same time cost either way
        return False
    return hmac.compare_digest(hash_pw(pw, u["salt"])[1], u["hash"])


def pw_problem(pw):
    if len(pw or "") < 8:
        return "Use at least 8 characters."
    return None


def clean_username(u):
    return re.sub(r"[^a-z0-9._-]", "", (u or "").strip().lower())[:40]


# ---------------------------------------------------------------- http helpers
def http(method, url, headers=None, body=None, timeout=60, raw=False):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        headers = dict(headers or {}, **{"Content-Type": "application/json"})
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, (data if raw else (json.loads(data) if data.strip() else None))
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except Exception:
            return e.code, data.decode("utf-8", "replace")[:500]


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


# ---------------------------------------------------------------- GitHub
def gh_ready():
    g = CONFIG.get("github", {})
    return g.get("repo") and g.get("token")


def gh(method, path, body=None, raw=False):
    g = CONFIG["github"]
    h = {"Authorization": "Bearer " + g["token"], "X-GitHub-Api-Version": "2022-11-28",
         "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
         "User-Agent": "ashtonsorigami"}
    url = "https://api.github.com/repos/" + g["repo"] + ("/" + path if path else "")
    return http(method, url, h, body, raw=raw)


def gh_put_file(path, content_bytes, message):
    st, cur = gh("GET", "contents/" + path)
    body = {"message": message, "content": base64.b64encode(content_bytes).decode()}
    if st == 200 and isinstance(cur, dict):
        body["sha"] = cur["sha"]
    st, res = gh("PUT", "contents/" + path, body)
    if st not in (200, 201):
        raise RuntimeError("GitHub said %s: %s" % (st, res.get("message") if isinstance(res, dict) else res))


def notes_markdown(notes):
    out = ["# Improvement notes for Ashton's Origami", "",
           "Open notes are listed first. When a version completes notes, list their ids in app/completed-notes.json.", ""]
    for n in sorted(notes, key=lambda n: (n.get("done", False), n.get("at", 0))):
        out.append("## [%s] %s (%s)" % ("Done" if n.get("done") else "Open", n.get("type"), n.get("id")))
        out.append(n.get("text", ""))
        out.append("_Screen %s · %s · %s · %s_" % (n.get("screen"), time.strftime("%Y-%m-%d %H:%M", time.localtime(n.get("at", 0) / 1000)),
                                                   n.get("device"), n.get("by", "")))
        out.append("")
    return "\n".join(out)


def push_notes():
    with LOCK:
        notes = list(STORE["records"]["notes"].values())
        DIRTY["notes"] = False
    try:
        gh_put_file("notes/notes.json", json.dumps(notes, indent=1, ensure_ascii=False).encode(), "Improvement notes")
        gh_put_file("notes/NOTES.md", notes_markdown(notes).encode(), "Improvement notes")
        STATE["notesSent"] = int(time.time() * 1000)
        STATE.pop("notesError", None)
    except Exception as e:
        DIRTY["notes"] = True
        STATE["notesError"] = str(e)[:300]
    jsave("state.json", STATE)


def seed_repo():
    """If the repo has no app yet, upload this version so updates have a starting point."""
    st, _ = gh("GET", "contents/app/VERSION")
    if st == 200:
        return False
    for f in sorted(os.listdir(APP_DIR)):
        p = os.path.join(APP_DIR, f)
        if os.path.isfile(p) and not f.endswith(".pyc"):
            with open(p, "rb") as fh:
                gh_put_file("app/" + f, fh.read(), "Version %s" % VERSION)
    gh_put_file("README.md", b"# Ashton's Origami\n\nThe app lives in `app/`. Raise `app/VERSION` to release a new version; "
                b"the app offers it to the admin. Improvement notes arrive in `notes/`.\n", "README")
    return True


def check_github():
    if not gh_ready():
        return None
    st, v = gh("GET", "contents/app/VERSION", raw=True)
    if st == 404:
        try:
            seed_repo()
            st, v = gh("GET", "contents/app/VERSION", raw=True)
        except Exception as e:
            STATE["checkError"] = "Couldn't copy the app into GitHub: %s" % str(e)[:200]
    STATE["lastCheck"] = int(time.time() * 1000)
    if st != 200:
        STATE["checkError"] = "Couldn't read app/VERSION from %s (%s)." % (CONFIG["github"]["repo"], st)
        jsave("state.json", STATE)
        return None
    v = v.decode().strip()
    STATE.pop("checkError", None)
    STATE["available"] = v if vtuple(v) > vtuple(VERSION) else None
    jsave("state.json", STATE)
    return STATE["available"]


def fetch_github_files():
    st, listing = gh("GET", "contents/app")
    if st != 200 or not isinstance(listing, list):
        raise RuntimeError("Couldn't list the app folder on GitHub (%s)." % st)
    files = {}
    for item in listing:
        if item.get("type") == "file":
            s, data = gh("GET", "contents/app/" + item["name"], raw=True)
            if s != 200:
                raise RuntimeError("Couldn't download %s (%s)." % (item["name"], s))
            files[item["name"]] = data
    return files


# ---------------------------------------------------------------- versions
def files_from_zip(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if not n.endswith("/")]
    tops = [n for n in names if n.split("/")[-1] == "VERSION"]
    if not tops:
        raise RuntimeError("That zip has no VERSION file.")
    base = min(tops, key=len)[:-len("VERSION")]
    files = {}
    for n in names:
        if n.startswith(base):
            rel = n[len(base):]
            if "/" in rel or rel.startswith(".") or ".." in rel:
                continue
            files[rel] = z.read(n)
    return files


def install(files, source):
    for need in ("VERSION", "server.py", "index.html"):
        if need not in files:
            raise RuntimeError("The update is missing %s." % need)
    ver = files["VERSION"].decode().strip()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.\-]{0,30}", ver):
        raise RuntimeError("That version number looks wrong.")
    if ver == VERSION:
        raise RuntimeError("Version %s is already running." % ver)
    tmp = tempfile.mkdtemp(dir=VERS, prefix=".new-")
    for name, data in files.items():
        if re.fullmatch(r"[A-Za-z0-9._-]+", name):
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(data)
    py_compile.compile(os.path.join(tmp, "server.py"), doraise=True)
    dest = os.path.join(VERS, ver)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    completed = []
    if "completed-notes.json" in files:
        try:
            completed = [str(x) for x in json.loads(files["completed-notes.json"])]
        except Exception:
            completed = []
    with LOCK:
        n = 0
        for nid in completed:
            note = STORE["records"]["notes"].get(nid)
            if note and not note.get("done"):
                note["done"] = True
                note["_rev"] = bump()
                n += 1
        if n:
            save_store()
            DIRTY["notes"] = True
        STATE["justUpdated"] = {"version": ver, "completed": n}
    switch_to(ver, source)
    return ver


def switch_to(ver, source):
    hist = STATE.setdefault("history", [])
    hist.append({"version": ver, "at": int(time.time() * 1000), "source": source})
    STATE["pending"] = {"version": ver, "previous": VERSION}
    STATE["available"] = None
    jsave("state.json", STATE)
    tmp = CURRENT + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.join(VERS, ver), tmp)
    os.replace(tmp, CURRENT)
    prune()
    threading.Timer(1.0, lambda: os._exit(0)).start()   # run.py starts the new version


def prune():
    keep = []
    for h in reversed(STATE.get("history", [])):
        if h["version"] not in keep:
            keep.append(h["version"])
    keep = keep[:KEEP_VERSIONS] + [VERSION]
    for d in os.listdir(VERS):
        if not d.startswith(".") and d not in keep and os.path.isdir(os.path.join(VERS, d)):
            shutil.rmtree(os.path.join(VERS, d), ignore_errors=True)


def versions_list():
    out = []
    for d in os.listdir(VERS) if os.path.isdir(VERS) else []:
        p = os.path.join(VERS, d)
        if not d.startswith(".") and os.path.isfile(os.path.join(p, "VERSION")):
            out.append({"version": d, "at": int(os.path.getmtime(p) * 1000), "current": d == VERSION})
    return sorted(out, key=lambda x: vtuple(x["version"]), reverse=True)


# ---------------------------------------------------------------- Dropbox
def dbx_cfg():
    return CONFIG.setdefault("dropbox", {})


def dbx_token():
    d = dbx_cfg()
    if not d.get("refresh"):
        raise RuntimeError("Dropbox isn't connected.")
    if d.get("access") and d.get("exp", 0) > time.time() + 60:
        return d["access"]
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": d["refresh"],
                                   "client_id": d["appKey"]}).encode()
    st, res = http("POST", "https://api.dropboxapi.com/oauth2/token",
                   {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if st != 200:
        raise RuntimeError("Dropbox sign-in has expired. Connect Dropbox again in Settings.")
    d["access"], d["exp"] = res["access_token"], time.time() + int(res.get("expires_in", 3600))
    jsave("config.json", CONFIG)
    return d["access"]


def dbx(endpoint, args):
    st, res = http("POST", "https://api.dropboxapi.com/2/" + endpoint,
                   {"Authorization": "Bearer " + dbx_token()}, args)
    if st != 200:
        raise RuntimeError("Dropbox %s: %s" % (endpoint, res))
    return res


def dbx_upload(path, data):
    arg = json.dumps({"path": path, "mode": "overwrite", "mute": True})   # ASCII-escaped, as Dropbox needs
    st, res = http("POST", "https://content.dropboxapi.com/2/files/upload",
                   {"Authorization": "Bearer " + dbx_token(), "Dropbox-API-Arg": arg,
                    "Content-Type": "application/octet-stream"}, data)
    if st != 200:
        raise RuntimeError("Dropbox upload of %s failed: %s" % (path, res))


def dbx_download(path):
    st, data = http("POST", "https://content.dropboxapi.com/2/files/download",
                    {"Authorization": "Bearer " + dbx_token(), "Dropbox-API-Arg": json.dumps({"path": path})},
                    b"", raw=True)
    if st != 200:
        raise RuntimeError("Couldn't download %s from Dropbox." % path)
    return data


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "-", s or "").strip(" .")[:80] or "untitled"


def money(x):
    try:
        return "%.2f" % float(x)
    except (TypeError, ValueError):
        return ""


STATES = ["New order", "Accepted", "In production", "Ready for shipping", "Shipped"]


def order_total(o):
    c = 0
    for l in o.get("lines", []):
        try:
            c += round(float(l.get("price")) * 100) * int(l.get("qty") or 0)
        except (TypeError, ValueError):
            pass
    try:
        c += round(float(o.get("shipping") or 0) * 100)
    except (TypeError, ValueError):
        pass
    return "%.2f" % (c / 100)


def csv_bytes(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")   # BOM so Excel reads accents


def data_url_bytes(u):
    m = re.match(r"data:image/[a-z]+;base64,(.*)", u or "", re.S)
    return base64.b64decode(m.group(1)) if m else None


def dropbox_sync():
    d = dbx_cfg()
    if not d.get("refresh") or not d.get("folder"):
        return
    folder = d["folder"].rstrip("/")
    with LOCK:
        DIRTY["dropbox"] = False
        snap = json.loads(json.dumps(STORE))
    STATE["syncing"] = True
    try:
        orders = sorted(snap["records"]["orders"].values(), key=lambda o: o.get("number") or 0)
        rows = [["Order", "Status", "Customer", "Contact", "Address", "Due", "Items", "Shipping", "Total", "Paid",
                 "Tracking", "Created", "Shipped"]]
        for o in orders:
            items = "; ".join("%s x %s%s%s" % (l.get("qty"), l.get("name"),
                                                (" [" + " + ".join(l.get("colours") or []) + "]") if l.get("colours") else "",
                                                (" @ " + money(l.get("price"))) if l.get("price") not in ("", None) else " (to quote)")
                              for l in o.get("lines", []))
            st = o.get("stamps", {})
            fmt = lambda t: time.strftime("%Y-%m-%d", time.localtime(t / 1000)) if t else ""
            rows.append(["%04d" % (o.get("number") or 0), STATES[o.get("status", 0)], o.get("customer"), o.get("contact"),
                         o.get("address"), o.get("due"), items, money(o.get("shipping")), order_total(o),
                         "Yes" if o.get("paid") else "No", o.get("tracking"), fmt(o.get("created")), fmt(st.get("4"))])
        dbx_upload(folder + "/Orders.csv", csv_bytes(rows))
        prods = list(snap["records"]["products"].values())
        dbx_upload(folder + "/Products.csv", csv_bytes([["Product", "Description", "Price", "Offered"]] +
                   [[p.get("name"), p.get("desc"), money(p.get("price")), "Yes" if p.get("active") else "No"] for p in prods]))
        leads = sorted(snap["records"]["leads"].values(), key=lambda l: l.get("created") or 0)
        dbx_upload(folder + "/Interested customers.csv", csv_bytes([["Name", "Contact", "Interested in", "Status", "Follow up", "Notes", "Added"]] +
                   [[l.get("name"), l.get("contact"), l.get("interest"), l.get("status"), l.get("follow"), l.get("notes"),
                     time.strftime("%Y-%m-%d", time.localtime((l.get("created") or 0) / 1000))] for l in leads]))
        costs = sorted(snap["records"]["costs"].values(), key=lambda c: c.get("date") or "")
        dbx_upload(folder + "/Costs.csv", csv_bytes([["Date", "What", "Category", "Amount", "Notes"]] +
                   [[c.get("date"), c.get("desc"), c.get("cat"), money(c.get("amount")), c.get("notes")] for c in costs]))
        sent = STATE.setdefault("photoHashes", {})
        pics = [("Products/" + safe_name(p.get("name")) + ".jpg", p.get("photo")) for p in prods]
        pics += [("Receipts/" + safe_name((c.get("date") or "") + " " + (c.get("desc") or "")) + ".jpg", c.get("photo")) for c in costs]
        pics += [("Paper colours/" + safe_name(c.get("name")) + ".jpg", c.get("img")) for c in snap["docs"]["colours"]["data"]]
        for path, url in pics:
            b = data_url_bytes(url)
            if b:
                h = hashlib.sha1(b).hexdigest()
                if sent.get(path) != h:
                    dbx_upload(folder + "/Photos/" + path, b)
                    sent[path] = h
        backup = json.dumps(export_data(snap), ensure_ascii=False).encode()
        dbx_upload(folder + "/Backups/latest.json", backup)
        dbx_upload(folder + "/Backups/" + time.strftime("%Y-%m-%d") + ".json", backup)
        try:
            old = [e for e in dbx("files/list_folder", {"path": folder + "/Backups"})["entries"]
                   if re.fullmatch(r"\d{4}-\d\d-\d\d\.json", e["name"])]
            for e in sorted(old, key=lambda e: e["name"])[:-30]:
                dbx("files/delete_v2", {"path": e["path_lower"]})
        except Exception:
            pass
        STATE["lastSync"] = int(time.time() * 1000)
        STATE.pop("syncError", None)
    except Exception as e:
        DIRTY["dropbox"] = True
        STATE["syncError"] = str(e)[:300]
    STATE["syncing"] = False
    jsave("state.json", STATE)


def export_data(snap=None):
    s = snap or STORE
    clean = lambda r: {k: v for k, v in r.items() if k != "_rev"}
    return {"app": "ashtonsorigami", "exported": int(time.time() * 1000), "nextOrder": s["nextOrder"],
            "orders": [clean(r) for r in s["records"]["orders"].values()],
            "products": [clean(r) for r in s["records"]["products"].values()],
            "notes": [clean(r) for r in s["records"]["notes"].values()],
            "leads": [clean(r) for r in s["records"]["leads"].values()],
            "costs": [clean(r) for r in s["records"]["costs"].values()],
            "settings": s["docs"]["settings"]["data"], "colours": s["docs"]["colours"]["data"]}


def worker():
    last_check = 0
    while True:
        try:
            if time.time() - last_check > 6 * 3600 and gh_ready():
                last_check = time.time()
                check_github()
            if DIRTY["notes"] and gh_ready():
                push_notes()
            if DIRTY["dropbox"]:
                dropbox_sync()
        except Exception as e:
            print("worker:", e, file=sys.stderr)
        time.sleep(30)


# ---------------------------------------------------------------- data API
def public_status(user):
    d = dbx_cfg()
    st = {"dropbox": bool(d.get("refresh") and d.get("folder")), "dirty": DIRTY["dropbox"] or STATE.get("syncing", False),
          "lastSync": STATE.get("lastSync"), "syncError": STATE.get("syncError")}
    if user and USERS.get(user, {}).get("role") == "admin":
        st["available"] = STATE.get("available")
        st["justUpdated"] = STATE.get("justUpdated")
        st["failed"] = STATE.get("failed")
    return st


def snapshot():
    recs = {c: [dict(r) for r in STORE["records"][c].values()] for c in COLLS}
    docs = {k: {"rev": v["rev"], "data": v["data"]} for k, v in STORE["docs"].items()}
    return {"rev": STORE["rev"], "records": recs, "docs": docs}


def changes_since(since):
    if since < STORE.get("resetRev", 1):
        return {"reload": True, "rev": STORE["rev"]}
    ch = []
    for c in COLLS:
        for r in STORE["records"][c].values():
            if r.get("_rev", 0) > since:
                ch.append({"coll": c, "id": r["id"], "data": r})
    for t in STORE["tomb"]:
        if t[0] > since:
            ch.append({"coll": t[1], "id": t[2], "deleted": True})
    docs = {k: v for k, v in STORE["docs"].items() if v["rev"] > since}
    return {"rev": STORE["rev"], "changes": ch, "docs": docs}


def apply_saves(items, user):
    admin = USERS[user]["role"] == "admin"
    out = []
    for it in items:
        coll, rid, base = it.get("coll"), str(it.get("id", ""))[:40], int(it.get("base") or 0)
        if coll in DOCS:
            doc = STORE["docs"][coll]
            if coll == "settings" and not admin:
                out.append({"coll": coll, "error": "Only an admin can change Settings."})
                continue
            if base != doc["rev"]:
                out.append({"coll": coll, "conflict": True, "rev": doc["rev"], "data": doc["data"]})
                continue
            doc["data"], doc["rev"] = it["data"], bump()
            out.append({"coll": coll, "ok": True, "rev": doc["rev"]})
            continue
        if coll not in COLLS or not rid:
            out.append({"coll": coll, "id": rid, "error": "Unknown record."})
            continue
        recs = STORE["records"][coll]
        cur = recs.get(rid)
        if coll == "notes" and not admin and (cur or it.get("delete")):
            out.append({"coll": coll, "id": rid, "error": "Only an admin can change notes.", "data": cur})
            continue
        if (cur.get("_rev", 0) if cur else 0) != base:
            out.append({"coll": coll, "id": rid, "conflict": True, "data": cur, "deleted": cur is None})
            continue
        if it.get("delete"):
            if cur:
                del recs[rid]
                STORE["tomb"].append([bump(), coll, rid])
                STORE["tomb"] = STORE["tomb"][-2000:]
            out.append({"coll": coll, "id": rid, "ok": True, "deleted": True})
            continue
        rec = dict(it.get("data") or {})
        rec["id"] = rid
        if coll == "orders":
            rec["number"] = cur["number"] if cur else STORE["nextOrder"]
            if not cur:
                STORE["nextOrder"] += 1
        if coll == "notes" and not cur:
            rec["by"] = USERS[user]["name"]
            DIRTY["notes"] = True
        if coll == "notes" and cur:
            DIRTY["notes"] = True
        rec["_rev"] = bump()
        recs[rid] = rec
        out.append({"coll": coll, "id": rid, "ok": True, "data": rec})
    save_store()
    return out


# ---------------------------------------------------------------- web server
class H(BaseHTTPRequestHandler):
    server_version = "ashtonsorigami"
    MAX = 30 * 1024 * 1024

    def log_message(self, fmt, *a):
        if not self.path.startswith("/api/changes"):
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def send(self, code, obj=None, body=None, ctype="application/json", extra=None):
        b = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.MAX:
            raise ValueError("too big")
        return self.rfile.read(n)

    def jbody(self):
        b = self.body()
        return json.loads(b) if b else {}

    def user(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["ao_session"].value if "ao_session" in c else None
        s = SESSIONS.get(tok) if tok else None
        if s and s["exp"] > time.time() and s["user"] in USERS:
            return s["user"]
        return None

    def start_session(self, user):
        tok = secrets.token_urlsafe(32)
        with LOCK:
            now = time.time()
            for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
                del SESSIONS[k]
            SESSIONS[tok] = {"user": user, "exp": now + SESSION_DAYS * 86400}
            jsave("sessions.json", SESSIONS)
        return {"Set-Cookie": "ao_session=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (tok, SESSION_DAYS * 86400)}

    def me(self, u):
        x = USERS[u]
        return {"username": u, "name": x["name"], "role": x["role"]}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self.send(200, body=f.read(), ctype="text/html; charset=utf-8")
        if path.path == "/healthz":
            return self.send(200, {"ok": True, "version": VERSION})
        if not path.path.startswith("/api/"):
            return self.send(404, {"error": "Not found"})
        u = self.user()
        if path.path == "/api/me":
            return self.send(200, {"setup": not USERS, "user": self.me(u) if u else None, "version": VERSION,
                                   "appName": STORE["docs"]["settings"]["data"].get("name")})
        if not u:
            return self.send(401, {"error": "Please log in."})
        q = urllib.parse.parse_qs(path.query)
        with LOCK:
            if path.path == "/api/data":
                s = snapshot()
                s.update(me=self.me(u), version=VERSION, status=public_status(u))
                return self.send(200, s)
            if path.path == "/api/changes":
                c = changes_since(int((q.get("since") or ["0"])[0]))
                c["status"] = public_status(u)
                return self.send(200, c)
        if USERS[u]["role"] != "admin":
            return self.send(403, {"error": "Admins only."})
        if path.path == "/api/admin/backup":
            b = json.dumps(export_data(), ensure_ascii=False).encode()
            return self.send(200, body=b, extra={"Content-Disposition": 'attachment; filename="ashtonsorigami-backup-%s.json"' % time.strftime("%Y-%m-%d")})
        if path.path == "/api/admin/info":
            return self.send(200, self.admin_info())
        return self.send(404, {"error": "Not found"})

    def admin_info(self):
        g, d = CONFIG.get("github", {}), dbx_cfg()
        return {"users": [dict(self.me(k)) for k in sorted(USERS)],
                "github": {"repo": g.get("repo", ""), "tokenSet": bool(g.get("token")), "tokenEnd": (g.get("token") or "")[-4:]},
                "dropbox": {"appKey": d.get("appKey", ""), "connected": bool(d.get("refresh")), "folder": d.get("folder", ""),
                            "account": d.get("account", "")},
                "state": {k: STATE.get(k) for k in ("lastCheck", "checkError", "available", "notesSent", "notesError",
                                                    "lastSync", "syncError", "justUpdated", "failed")},
                "history": STATE.get("history", [])[-20:], "versions": versions_list(), "version": VERSION,
                "openNotes": sum(1 for n in STORE["records"]["notes"].values() if not n.get("done")),
                "updatedAt": next((h["at"] for h in reversed(STATE.get("history", [])) if h["version"] == VERSION), None)}

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            return self.send(404, {"error": "Not found"})
        if self.headers.get("X-AO") != "1":         # blocks cross-site form posts
            return self.send(403, {"error": "Bad request."})
        try:
            return self.post(path)
        except (RuntimeError, ValueError, OSError, zipfile.BadZipFile, py_compile.PyCompileError) as e:
            return self.send(400, {"error": str(e)[:400]})

    def post(self, path):
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        if path == "/api/setup":
            b = self.jbody()
            with LOCK:
                if USERS:
                    return self.send(400, {"error": "The first account already exists. Please log in."})
                user, pw = clean_username(b.get("username")), b.get("password", "")
                if not user:
                    return self.send(400, {"error": "Choose a username."})
                if pw_problem(pw):
                    return self.send(400, {"error": pw_problem(pw)})
                salt, h = hash_pw(pw)
                USERS[user] = {"name": (b.get("name") or user).strip()[:40], "role": "admin", "salt": salt, "hash": h}
                jsave("users.json", USERS)
            return self.send(200, {"ok": True}, extra=self.start_session(user))
        if path == "/api/login":
            b = self.jbody()
            user = clean_username(b.get("username"))
            key = ip + "|" + user
            f = FAILS.get(key, [0, 0])
            if f[0] >= 5 and time.time() - f[1] < 60:
                return self.send(429, {"error": "Too many tries. Wait a minute and try again."})
            if not check_pw(user, b.get("password", "")):
                FAILS[key] = [f[0] + 1, time.time()]
                return self.send(401, {"error": "That username or password isn't right."})
            FAILS.pop(key, None)
            return self.send(200, {"ok": True}, extra=self.start_session(user))
        if path == "/api/logout":
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            with LOCK:
                if "ao_session" in c and SESSIONS.pop(c["ao_session"].value, None):
                    jsave("sessions.json", SESSIONS)
            return self.send(200, {"ok": True}, extra={"Set-Cookie": "ao_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"})
        u = self.user()
        if not u:
            return self.send(401, {"error": "Please log in again."})
        if path == "/api/save":
            b = self.jbody()
            with LOCK:
                return self.send(200, {"results": apply_saves(b.get("items", []), u), "rev": STORE["rev"]})
        if path == "/api/password":
            b = self.jbody()
            if not check_pw(u, b.get("old", "")):
                return self.send(400, {"error": "Your current password isn't right."})
            if pw_problem(b.get("new")):
                return self.send(400, {"error": pw_problem(b.get("new"))})
            with LOCK:
                USERS[u]["salt"], USERS[u]["hash"] = hash_pw(b["new"])
                jsave("users.json", USERS)
                mine = self.headers.get("Cookie", "")
                for k in [k for k, v in SESSIONS.items() if v["user"] == u and k not in mine]:
                    del SESSIONS[k]          # log out other devices
                jsave("sessions.json", SESSIONS)
            return self.send(200, {"ok": True})
        if USERS[u]["role"] != "admin":
            return self.send(403, {"error": "Admins only."})
        return self.admin_post(path, u)

    def admin_post(self, path, u):
        if path == "/api/admin/users":
            b = self.jbody()
            act, name = b.get("action"), clean_username(b.get("username"))
            with LOCK:
                admins = [k for k, v in USERS.items() if v["role"] == "admin"]
                if act == "add":
                    if not name:
                        raise ValueError("Choose a username (letters and numbers).")
                    if name in USERS:
                        raise ValueError("That username is taken.")
                    if pw_problem(b.get("password")):
                        raise ValueError(pw_problem(b.get("password")))
                    salt, h = hash_pw(b["password"])
                    USERS[name] = {"name": (b.get("name") or name).strip()[:40], "role": "admin" if b.get("role") == "admin" else "user",
                                   "salt": salt, "hash": h}
                elif name not in USERS:
                    raise ValueError("No such user.")
                elif act == "role":
                    role = "admin" if b.get("role") == "admin" else "user"
                    if role == "user" and admins == [name]:
                        raise ValueError("There must always be at least one admin.")
                    USERS[name]["role"] = role
                elif act == "reset":
                    if pw_problem(b.get("password")):
                        raise ValueError(pw_problem(b.get("password")))
                    USERS[name]["salt"], USERS[name]["hash"] = hash_pw(b["password"])
                    for k in [k for k, v in SESSIONS.items() if v["user"] == name]:
                        del SESSIONS[k]
                elif act == "remove":
                    if name == u:
                        raise ValueError("You can't remove yourself.")
                    if admins == [name]:
                        raise ValueError("There must always be at least one admin.")
                    del USERS[name]
                    for k in [k for k, v in SESSIONS.items() if v["user"] == name]:
                        del SESSIONS[k]
                else:
                    raise ValueError("Unknown action.")
                jsave("users.json", USERS)
                jsave("sessions.json", SESSIONS)
            return self.send(200, self.admin_info())
        if path == "/api/admin/github":
            b = self.jbody()
            repo = (b.get("repo") or "").strip().strip("/")
            repo = re.sub(r"^https?://github\.com/", "", repo)
            if repo and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo):
                raise ValueError("Write the repository as owner/name, e.g. nadiadubeau/ashtonsorigami.")
            g = CONFIG.setdefault("github", {})
            g["repo"] = repo
            if (b.get("token") or "").strip():
                g["token"] = b["token"].strip()
            jsave("config.json", CONFIG)
            msg = "Saved."
            if gh_ready():
                st, res = gh("GET", "")
                if st != 200:
                    raise ValueError("Saved, but GitHub refused: %s. Check the repository name and that the token can read and write Contents." %
                                     (res.get("message") if isinstance(res, dict) else st))
                if seed_repo():
                    msg = "Connected. I copied this version (%s) into the repository so updates can start from it." % VERSION
                else:
                    msg = "Connected to GitHub."
                DIRTY["notes"] = True
                check_github()
            return self.send(200, {"message": msg, "info": self.admin_info()})
        if path == "/api/admin/check":
            if not gh_ready():
                raise ValueError("Add the repository and token first.")
            v = check_github()
            return self.send(200, {"available": v, "info": self.admin_info()})
        if path == "/api/admin/install-github":
            ver = install(fetch_github_files(), "GitHub")
            return self.send(200, {"installing": ver})
        if path == "/api/admin/install-zip":
            ver = install(files_from_zip(self.body()), "file")
            return self.send(200, {"installing": ver})
        if path == "/api/admin/dropbox-updates":
            b = self.jbody()
            folder = dbx_cfg().get("folder", "").rstrip("/") + "/_App updates"
            if b.get("name"):
                ver = install(files_from_zip(dbx_download(folder + "/" + b["name"])), "Dropbox")
                return self.send(200, {"installing": ver})
            try:
                entries = dbx("files/list_folder", {"path": folder})["entries"]
            except RuntimeError:
                dbx("files/create_folder_v2", {"path": folder})
                entries = []
            return self.send(200, {"files": sorted([e["name"] for e in entries if e["name"].lower().endswith(".zip")], reverse=True)})
        if path == "/api/admin/rollback":
            ver = self.jbody().get("version", "")
            if ver == VERSION or not os.path.isfile(os.path.join(VERS, ver, "VERSION")):
                raise ValueError("That version isn't available.")
            switch_to(ver, "Went back")
            return self.send(200, {"installing": ver})
        if path == "/api/admin/dismiss":
            STATE.pop("justUpdated", None)
            STATE.pop("failed", None)
            jsave("state.json", STATE)
            return self.send(200, {"ok": True})
        if path == "/api/admin/restore":
            b = self.jbody()
            if b.get("app") != "ashtonsorigami" or not isinstance(b.get("orders"), list):
                raise ValueError("That isn't a backup from this app.")
            with LOCK:
                rev = bump()
                for c in COLLS:
                    STORE["records"][c] = {}
                    for r in b.get(c, []):
                        if isinstance(r, dict) and r.get("id"):
                            r["_rev"] = rev
                            STORE["records"][c][str(r["id"])] = r
                STORE["nextOrder"] = max([int(b.get("nextOrder") or 1)] + [(o.get("number") or 0) + 1 for o in b["orders"]])
                STORE["docs"]["settings"] = {"rev": rev, "data": b.get("settings") or STORE["docs"]["settings"]["data"]}
                STORE["docs"]["colours"] = {"rev": rev, "data": b.get("colours") or []}
                STORE["tomb"], STORE["resetRev"] = [], rev
                save_store()
            return self.send(200, {"ok": True})
        if path == "/api/dropbox/start":
            key = (self.jbody().get("appKey") or dbx_cfg().get("appKey") or "").strip()
            if not re.fullmatch(r"[a-z0-9]{8,30}", key):
                raise ValueError("Paste the Dropbox app key first.")
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            d = dbx_cfg()
            d["appKey"], d["verifier"] = key, verifier
            jsave("config.json", CONFIG)
            url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
                {"client_id": key, "response_type": "code", "code_challenge": challenge,
                 "code_challenge_method": "S256", "token_access_type": "offline"})
            return self.send(200, {"url": url})
        if path == "/api/dropbox/finish":
            d = dbx_cfg()
            code = (self.jbody().get("code") or "").strip()
            body = urllib.parse.urlencode({"code": code, "grant_type": "authorization_code", "client_id": d.get("appKey", ""),
                                           "code_verifier": d.get("verifier", "")}).encode()
            st, res = http("POST", "https://api.dropboxapi.com/oauth2/token", {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if st != 200 or not isinstance(res, dict) or "refresh_token" not in res:
                raise ValueError("Dropbox didn't accept that code. Press Start again and paste the new code.")
            d.update(refresh=res["refresh_token"], access=res["access_token"], exp=time.time() + int(res.get("expires_in", 3600)))
            d.pop("verifier", None)
            try:
                d["account"] = dbx("users/get_current_account", None)["email"]
            except Exception:
                d["account"] = ""
            jsave("config.json", CONFIG)
            return self.send(200, self.admin_info())
        if path == "/api/dropbox/disconnect":
            d = dbx_cfg()
            try:
                dbx("auth/token/revoke", None)
            except Exception:
                pass
            for k in ("refresh", "access", "exp", "account"):
                d.pop(k, None)
            jsave("config.json", CONFIG)
            return self.send(200, self.admin_info())
        if path == "/api/dropbox/list":
            p = self.jbody().get("path", "")
            res = dbx("files/list_folder", {"path": p})
            entries = res["entries"]
            while res.get("has_more") and len(entries) < 2000:
                res = dbx("files/list_folder/continue", {"cursor": res["cursor"]})
                entries += res["entries"]
            return self.send(200, {"folders": sorted([e["path_display"] for e in entries if e[".tag"] == "folder"], key=str.lower)})
        if path == "/api/dropbox/mkdir":
            p = self.jbody().get("path", "")
            dbx("files/create_folder_v2", {"path": p})
            return self.send(200, {"ok": True})
        if path == "/api/dropbox/folder":
            d = dbx_cfg()
            d["folder"] = self.jbody().get("path", "")
            jsave("config.json", CONFIG)
            DIRTY["dropbox"] = True
            threading.Thread(target=dropbox_sync, daemon=True).start()
            return self.send(200, self.admin_info())
        if path == "/api/dropbox/sync":
            dropbox_sync()
            if STATE.get("syncError"):
                raise ValueError(STATE["syncError"])
            return self.send(200, self.admin_info())
        return self.send(404, {"error": "Not found"})


def main():
    p = STATE.get("pending")
    if p and p.get("version") == VERSION:
        def ok():
            STATE.pop("pending", None)
            jsave("state.json", STATE)
        threading.Timer(20, ok).start()
    if not STATE.get("history"):
        STATE["history"] = [{"version": VERSION, "at": int(time.time() * 1000), "source": "First install"}]
        jsave("state.json", STATE)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print("Ashton's Origami %s on %s:%d" % (VERSION, HOST, PORT), file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
