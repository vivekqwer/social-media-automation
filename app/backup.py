"""Backup and restore of one private copy (the owner's or a customer's), from the dashboard.

* make()      the copy's data as a dict (every table except the YouTube numbers, faces as base64; channel logins
              stay encrypted, so they only work again on this same server)
* pack()      that dict as a .json.gz file
* daily()     timed job: one automatic backup a day per account in DATA_DIR/backups, the last 7 kept
* read()      a file someone uploads, checked (size, gzip bomb, format, version, tables) before anything happens
* restore()   replaces this copy's tables in ONE transaction, after a safety backup of what is there now

Only the copy the running code works in is ever read or written (engine_for() of the current customer). The shared
tables (accounts, activity) are not in Base.metadata, so they are never part of a backup or a restore.
"""
import base64
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import tenant
from .config import DATA_DIR
from .db import IS_SQLITE, Base, engine_for, utcnow

log = logging.getLogger(__name__)

FORMAT = "raji-studio-backup"
VERSION = 1
SKIP = ("video_stats", "channel_stats")  # read from YouTube again
KEEP_AUTO = 7  # automatic daily backups kept per account
KEEP_SAFETY = 3  # safety backups made just before a restore
MAX_AUTO = 50 * 1024 * 1024  # an automatic backup bigger than this is not saved (an alert says so)
MAX_TOTAL = 150 * 1024 * 1024  # all backups of one account together
MAX_UPLOAD = 100 * 1024 * 1024  # a file sent for restore (Super Admin)
MAX_UNPACKED = 300 * 1024 * 1024  # what it may grow to when unpacked (gzip bomb protection; Super Admin)
MAX_UPLOAD_USER = 20 * 1024 * 1024  # the same for customers: one upload must never be able to take the server down
MAX_UNPACKED_USER = 50 * 1024 * 1024
RATE = {"check": 6, "restore": 3}  # per customer account and hour
BUSY = "The system is busy right now — nothing was changed. Try again in a minute."
INT_RANGE = (-2 ** 31, 2 ** 31 - 1)  # Integer columns on PostgreSQL
BIG_RANGE = (-2 ** 63, 2 ** 63 - 1)  # BigInteger
NOT_YOURS = "This backup was not made by this server for your account. Ask the person who runs the system to restore it."
MAX_ROWS = 500_000  # per table
MAX_FACE = 3_000_000
NAME_RE = re.compile(r"^(auto|safety)-\d{8}-\d{6}\.json\.gz$")
FACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.png$")
# settings that belong to the server, not to the data: never taken from a file, the current value stays
# (passwords and sign-ins, Super Admin decisions, daily allowance counters)
PROTECTED = {"admin_pw", "admin_emails", "session_gen", "reset_state", "oauth_state", "self_test", "shared_google",
             "onboarding", "acl_plans", "pay_info", "razorpay", "plan_prices",
             "strike_blocked", "strike_watch_init",  # the never-use list after a strike: an older backup must not bring a struck creator back
             "helper_enroll", "helper_removed"}  # helper key: an old backup must not bring back a key made new or switched off
PROTECTED_PREFIX = ("quota", "gmail_sent:", "gmail_owner:", "strike_sent:")


class BackupError(Exception):
    """A plain-English reason why a backup file cannot be used."""


def _protected(key) -> bool:
    k = str(key or "")
    return k in PROTECTED or k.startswith(PROTECTED_PREFIX)


def tables():
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP]


def brand(tid: int | None = None) -> str:
    tid = tenant.current() if tid is None else int(tid)
    if tid == tenant.OWNER:
        return "Raji Studio"
    try:
        from . import accounts, acl

        aid = acl._account_of(tid)
        a = accounts.get(aid) if aid else None
        return (a.brand or a.name or "") if a else ""
    except Exception:  # noqa: BLE001
        return ""


def _sign_key() -> bytes:
    """Never leaves the server: derived from the key that also encrypts the channel logins (DATA_DIR/.token_key or
    TOKEN_ENCRYPTION_KEY), not from anything a backup could carry or a restore could overwrite."""
    from .crypto import _key

    return hashlib.sha256(b"raji-backup-signature|" + _key()).digest()


def _mac(data: dict) -> str:
    body = json.dumps({k: v for k, v in data.items() if k != "signature"}, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=True).encode()
    msg = f"{data.get('format')}|{data.get('version')}|{data.get('tenant')}|{hashlib.sha256(body).hexdigest()}"
    return hmac.new(_sign_key(), msg.encode(), hashlib.sha256).hexdigest()


def sign(data: dict) -> dict:
    data.pop("signature", None)
    data["signature"] = {"alg": "hmac-sha256", "mac": _mac(data)}
    return data


def signed(data: dict) -> bool:
    """Made by THIS server and not changed since (format, version, account number and every row)."""
    s = data.get("signature")
    if not isinstance(s, dict) or not isinstance(s.get("mac"), str):
        return False
    try:
        return hmac.compare_digest(s["mac"], _mac(data))
    except Exception:  # noqa: BLE001
        return False


_hits: dict[tuple[int, str], list[float]] = {}
_hits_lock = threading.Lock()


def allow(kind: str, tid: int | None = None) -> bool:
    """Customers: at most RATE[kind] checks / restores an hour per account."""
    tid = tenant.current() if tid is None else int(tid)
    now = time.time()
    with _hits_lock:
        seen = [x for x in _hits.get((tid, kind), []) if now - x < 3600]
        if len(seen) >= RATE[kind]:
            _hits[(tid, kind)] = seen
            return False
        seen.append(now)
        _hits[(tid, kind)] = seen
        return True


def _value(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def make() -> dict:
    """Everything needed to rebuild this copy's dashboard (channel logins stay encrypted; videos are on YouTube)."""
    tid = tenant.current()
    data = {"format": FORMAT, "version": VERSION, "made_at": utcnow().isoformat() + "Z", "tenant": tid, "brand": brand(tid),
            "app_commit": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:40], "tables": {}}
    conn = engine_for(tid).connect()
    if not IS_SQLITE:  # every table read from the same moment, even while jobs write
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
    with conn, conn.begin():
        for t in tables():
            rows = conn.execute(t.select().order_by(*t.primary_key.columns)).mappings().all()
            data["tables"][t.name] = [{k: _value(v) for k, v in r.items()} for r in rows]
    data["counts"] = {k: len(v) for k, v in data["tables"].items()}
    faces = DATA_DIR / "faces"
    if faces.exists():
        data["faces"] = {f.name: base64.b64encode(f.read_bytes()).decode() for f in sorted(faces.glob("*.png"))
                         if f.stat().st_size < MAX_FACE and FACE_RE.match(f.name)}
    return sign(data)


def pack(data: dict) -> bytes:
    return gzip.compress(json.dumps(data, separators=(",", ":"), default=str).encode("utf-8"), compresslevel=6)


def file_name(data: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (data.get("brand") or "raji-studio").lower()).strip("-")[:40] or "raji-studio"
    return f"{slug}-backup-{utcnow().strftime('%Y-%m-%d-%H%M')}.json.gz"


# ---------------------------------------------------------------- the files kept on the server
def folder():
    return DATA_DIR / "backups"


def _write(kind: str, blob: bytes) -> str:
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{kind}-{utcnow().strftime('%Y%m%d-%H%M%S')}.json.gz"
    tmp = d / (name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, d / name)
    _rotate()
    return name


def _rotate():
    d = folder()
    for kind, keep in (("auto", KEEP_AUTO), ("safety", KEEP_SAFETY)):
        for old in sorted(d.glob(kind + "-*.json.gz"))[:-keep]:
            old.unlink(missing_ok=True)
    files = sorted((f for f in d.glob("*.json.gz") if NAME_RE.match(f.name)), key=lambda f: f.name.split("-", 1)[1])
    total = sum(f.stat().st_size for f in files)
    while total > MAX_TOTAL and len(files) > 1:  # the newest one always stays
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
    for f in d.glob("*.tmp"):  # a write that was cut off
        if time.time() - f.stat().st_mtime > 3600:
            f.unlink(missing_ok=True)
    p = d / "upload-pending.json.gz"
    if p.exists() and time.time() - p.stat().st_mtime > 3600:
        p.unlink(missing_ok=True)


def listing() -> list[dict]:
    d = folder()
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.json.gz"):
        if not NAME_RE.match(f.name):
            continue
        stamp = f.name.split("-", 1)[1][:15]
        at = datetime.strptime(stamp, "%Y%m%d-%H%M%S").isoformat() + "Z"
        out.append({"name": f.name, "kind": f.name.split("-", 1)[0], "size": f.stat().st_size, "at": at})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def path_of(name: str):
    if not NAME_RE.match(name or ""):
        raise BackupError("This backup does not exist.")
    p = folder() / name
    if not p.exists():
        raise BackupError("This backup does not exist (it may have been replaced by a newer one).")
    return p


def daily(force: bool = False) -> str | None:
    """Timed job (per account): one automatic backup a day. Never videos — only the data, a few MB."""
    today = utcnow().strftime("%Y%m%d")
    d = folder()
    if not force and d.exists() and any(d.glob(f"auto-{today}-*.json.gz")):
        return None
    try:
        blob = pack(make())
        if len(blob) > MAX_AUTO:
            raise BackupError(f"The daily backup is {len(blob) // (1024 * 1024)} MB — more than the 50 MB kept on the server, "
                              "so it was not saved. Use My channels → Backup & restore → Download backup now instead.")
        return _write("auto", blob)
    except Exception as e:  # noqa: BLE001
        log.warning("daily backup: %s", str(e)[:200])
        from .alerts import note

        note("Daily backup", e if isinstance(e, BackupError) else BackupError(f"The daily backup could not be made: {str(e)[:200]}"))
        return None


def safety() -> str:
    return _write("safety", pack(make()))


# ---------------------------------------------------------------- reading a file sent for restore
_unpack_one = threading.BoundedSemaphore(1)  # customers' files are unpacked one at a time, server-wide


def unpack(blob: bytes, is_super: bool = True) -> dict:
    """Read a file sent for restore. A customer's file waits for its turn, so several large files sent at the same
    moment can never be in memory together."""
    if is_super:
        return _unpack(blob, True)
    if not _unpack_one.acquire(timeout=20):
        raise BackupError("The system is busy checking another backup right now. Try again in a minute.")
    try:
        return _unpack(blob, False)
    finally:
        _unpack_one.release()


def _unpack(blob: bytes, is_super: bool = True) -> dict:
    cap, cap_raw = (MAX_UPLOAD, MAX_UNPACKED) if is_super else (MAX_UPLOAD_USER, MAX_UNPACKED_USER)
    if not blob:
        raise BackupError("The file is empty.")
    if len(blob) > cap:
        if not is_super and len(blob) <= MAX_UPLOAD:
            raise BackupError(f"This backup is larger than {cap // (1024 * 1024)} MB, too big to restore from your dashboard. "
                              "Ask the person who runs the system (the Super Admin) to restore it for you.")
        raise BackupError("The file is larger than 100 MB. A backup of Raji Studio is normally a few MB — is this the right file?")
    raw = blob
    if blob[:2] == b"\x1f\x8b":
        buf = bytearray()
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(blob)) as g:
                while True:  # never more than the cap in memory, whatever the file claims
                    part = g.read(min(1024 * 1024, cap_raw + 1 - len(buf)))
                    if not part:
                        break
                    buf += part
                    if len(buf) > cap_raw:
                        break
        except (OSError, EOFError) as e:
            raise BackupError("The file is damaged (it could not be unpacked).") from e
        if len(buf) > cap_raw:
            if not is_super:
                raise BackupError(f"The file unpacks to more than {cap_raw // (1024 * 1024)} MB, too big to restore from your dashboard. "
                                  "Ask the person who runs the system (the Super Admin) to restore it for you.")
            raise BackupError("The file unpacks to more than 300 MB. That is not a Raji Studio backup.")
        raw = bytes(buf)
    elif len(blob) > cap_raw:
        raise BackupError("The file is too large to be a Raji Studio backup.")
    if raw.lstrip()[:1] != b"{":
        raise BackupError("This is not a Raji Studio backup file. Choose the .json.gz file you downloaded.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise BackupError("The file is damaged (its contents cannot be read).") from e
    check(data)
    return data


def check(data) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise BackupError("This is not a Raji Studio backup file. Choose the .json.gz file you downloaded.")
    fmt = data.get("format")
    if fmt is not None and fmt != FORMAT:
        raise BackupError("This file is a backup of another program, not of Raji Studio.")
    ver = data.get("version", 0)
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 0:
        raise BackupError("The file is damaged (unknown version).")
    if ver > VERSION:
        raise BackupError("This backup was made by a newer version of Raji Studio. Update the server first, then restore.")
    known = {t.name for t in tables()}
    if not known & set(data["tables"]):
        raise BackupError("The file contains none of Raji Studio's data.")
    for name, rows in data["tables"].items():
        if name in known and (not isinstance(rows, list) or len(rows) > MAX_ROWS or not all(isinstance(r, dict) for r in rows)):
            raise BackupError(f"The file is damaged (the part “{name}” is not readable).")
    faces = data.get("faces") or {}
    if not isinstance(faces, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in faces.items()):
        raise BackupError("The file is damaged (the face photos are not readable).")
    tid = data.get("tenant")
    if tid is not None and (not isinstance(tid, int) or isinstance(tid, bool)):
        raise BackupError("The file is damaged (unknown account number).")


def current_counts() -> dict:
    from sqlalchemy import func, select

    with engine_for().connect() as conn:
        return {t.name: conn.execute(select(func.count()).select_from(t)).scalar() or 0 for t in tables()}


def current_faces() -> int:
    faces = DATA_DIR / "faces"
    if not faces.exists():
        return 0
    return sum(1 for f in faces.glob("*.png") if FACE_RE.match(f.name) and f.stat().st_size < MAX_FACE)


def preview(data: dict, is_super: bool) -> dict:
    """What the file contains and what restoring it would do (nothing is changed)."""
    tid = tenant.current()
    known = {t.name for t in tables()}
    owner_of = data.get("tenant")
    counts = {t.name: len(data["tables"].get(t.name) or []) for t in tables()}
    out = {"made_at": data.get("made_at"), "brand": data.get("brand") or "", "tenant": owner_of, "this_tenant": tid,
           "version": data.get("version", 0), "app_commit": data.get("app_commit") or "", "counts": counts,
           "current": current_counts(), "faces": len(data.get("faces") or {}), "faces_now": current_faces(), "warnings": [], "same_account": owner_of == tid}
    other = sorted(set(data["tables"]) - known - set(SKIP))
    if other:
        out["warnings"].append("Parts this version does not know are skipped: " + ", ".join(other[:10]) + ".")
    out["signed"] = signed(data)
    if not is_super and not (out["signed"] and owner_of == tid):
        raise BackupError(NOT_YOURS)
    if not out["signed"]:
        out["warnings"].append("This file was not made by this server, or it was changed after it was made (or it comes from an "
                               "older version). Restore it only if you trust where it came from.")
    if owner_of is None:
        out["warnings"].append("This file does not say which account it was made for (older version). Make sure it is the right one.")
    elif owner_of != tid:
        out["warnings"].append(f"WARNING: this backup was made for another account (copy #{owner_of}"
                               f"{', “' + data.get('brand') + '”' if data.get('brand') else ''}), but you are restoring it into "
                               f"copy #{tid}{' (your own system)' if tid == tenant.OWNER else ''}. Everything in copy #{tid} will be replaced.")
    if not is_super:
        _limits(data)
    out["notes"] = (["Your helper PCs stay as they are now (approve new ones in My channels)."] if not is_super else []) + ["Videos are not in a backup. On the same server they are still there. On a new server, make the waiting clips again "
                    "(Clips → Make all waiting clips again) before they are uploaded.",
                    "Channel logins in a backup only work on this same server. After restoring on a new server, press Reconnect on each channel."]
    return out


def _limits(data: dict):
    from . import acl

    cap = acl.limit("channels")
    n = len(data["tables"].get("destinations") or [])
    if cap and n > cap:
        raise BackupError(f"This backup has {n} YouTube channels and your plan allows {cap}. Ask for more channels first.")
    cap = acl.limit("sources")
    n = sum(1 for r in data["tables"].get("channels") or [] if r.get("kind") != "viral")
    if cap and n > cap:
        raise BackupError(f"This backup has {n} sources and your account can have {cap}. {acl.ASK.capitalize()} for more.")


# ---------------------------------------------------------------- restore
def _default(col):
    d = col.default
    if d is None:
        return None
    if getattr(d, "is_scalar", False):
        return d.arg
    if getattr(d, "is_callable", False):
        try:
            return d.arg(None)
        except Exception:  # noqa: BLE001
            return None
    return None


def _has_nul(v) -> bool:
    if isinstance(v, str):
        return "\x00" in v
    if isinstance(v, dict):
        return any(_has_nul(k) or _has_nul(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_has_nul(x) for x in v)
    return False


def _cell(table, col, v, i):
    from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String

    if v is None:
        return None
    where = f"the part “{table}”, row {i + 1}, “{col.name}”"
    t = col.type
    if _has_nul(v):
        raise BackupError(f"The file is damaged ({where} contains an invisible NUL character).")
    try:
        if isinstance(t, DateTime):
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        if isinstance(t, Boolean):
            if isinstance(v, bool):
                return v
            if v in (0, 1):
                return bool(v)
            raise ValueError
        if isinstance(t, Integer):
            if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                raise ValueError
            n = int(v)
            lo, hi = BIG_RANGE if isinstance(t, BigInteger) else INT_RANGE
            if not lo <= n <= hi:
                raise BackupError(f"The file is damaged ({where} has a number that is too large: {str(n)[:30]}).")
            return n
        if isinstance(t, Float):
            if isinstance(v, bool):
                raise ValueError
            return float(v)
        if isinstance(t, JSON):
            return v
        if isinstance(t, String):  # Text too
            s = v if isinstance(v, str) else json.dumps(v)
            return s[:t.length] if t.length else s
    except (ValueError, TypeError, OverflowError) as e:
        raise BackupError(f"The file is damaged ({where} has a wrong value).") from e
    return v


def _rows(table, rows: list) -> list[dict]:
    from sqlalchemy import JSON, null

    out = []
    pk = [c.name for c in table.primary_key.columns]
    for i, r in enumerate(rows):
        row = {}
        for col in table.columns:
            v = _cell(table.name, col, r[col.name], i) if col.name in r else _default(col)
            row[col.name] = null() if v is None and isinstance(col.type, JSON) else v  # SQL NULL, not the JSON word null
        if any(row[k] is None for k in pk):
            raise BackupError(f"The file is damaged (the part “{table.name}”, row {i + 1} has no number).")
        out.append(row)  # columns the file has but this version does not are dropped
    return out


def _check_links(prepared: dict):
    """A row that points to another row (a clip to its project, …) must find it in the file — checked for every
    foreign key of the tables, before anything is changed (PostgreSQL would refuse it half-way, SQLite would accept it)."""
    names = {t.name for t in tables()}
    for t in tables():
        for fk in t.foreign_keys:
            ref, ref_col, col = fk.column.table.name, fk.column.name, fk.parent.name
            if ref not in names:
                continue
            have = {r.get(ref_col) for r in prepared.get(ref) or []}
            for i, r in enumerate(prepared.get(t.name) or []):
                v = r.get(col)
                if v is not None and not hasattr(v, "compile") and v not in have:
                    raise BackupError(f"The file is damaged (the part “{t.name}”, row {i + 1}: “{col}” is {v}, but there is no "
                                      f"such row in the part “{ref}”). Nothing was changed.")


def _busy(e) -> bool:
    o = getattr(e, "orig", None)
    return (getattr(o, "pgcode", None) or getattr(o, "sqlstate", None)) in ("55P03", "40P01") or "lock timeout" in str(e).lower()


def _reset_sequences(conn, tid: int):
    if IS_SQLITE:
        return
    from sqlalchemy import Integer, text

    sch = tenant.schema(tid)
    for t in tables():
        pk = list(t.primary_key.columns)
        if len(pk) != 1 or not isinstance(pk[0].type, Integer):
            continue
        full = f'"{sch}".{t.name}' if sch else t.name
        conn.execute(text(f"SELECT setval(pg_get_serial_sequence(:t, :c), COALESCE((SELECT MAX({pk[0].name}) FROM {full}), 0) + 1, false)"),
                     {"t": full, "c": pk[0].name})


def restore(data: dict, is_super: bool) -> dict:
    """Replace this copy's data with the file's. A safety backup of the current state is made first.
    Background work of this copy waits meanwhile (jobs.RESTORING)."""
    from . import jobs

    check(data)
    tid = tenant.current()
    preview(data, is_super)  # same refusals as the preview (other account, limits)
    if not jobs.start_restore(tid):
        raise BackupError("A restore is already running for this account. Wait a minute and look again.")
    try:
        if not jobs.wait_idle(tid, 120):
            raise BackupError("The system is busy with this account's videos right now (making or uploading a clip). "
                              "Nothing was changed. Try again in a few minutes.")
        safe = safety()
        prepared = {t.name: _rows(t, data["tables"].get(t.name) or []) for t in tables()}
        faces = _faces(data.get("faces") or {})
        _check_links(prepared)
        settings = next(t for t in tables() if t.name == "settings")
        kept_uploaded = 0
        from sqlalchemy.exc import DBAPIError

        try:
            kept_uploaded = _replace(tid, data, prepared, settings, is_super)
        except DBAPIError as e:
            if _busy(e):  # a row is held by another connection: give up instead of waiting forever
                raise BackupError(BUSY) from e
            raise
        try:
            _put_faces(faces)
        except OSError as e:  # the data is restored already; only the photos are missing
            from .alerts import note

            note("Restore a backup", BackupError(f"The data was restored, but the face photos could not be saved: {str(e)[:150]}. "
                                                 "Upload your face photo again in My channels."))
    finally:
        jobs.end_restore(tid)
    _after(tid)
    return {"restored": {k: len(v) for k, v in prepared.items()}, "faces": len(faces), "safety": safe, "kept_uploaded": kept_uploaded}


def _replace(tid: int, data: dict, prepared: dict, settings, is_super: bool) -> int:
    """Delete this copy's rows and insert the file's, in one transaction (all or nothing)."""
    from sqlalchemy import text

    kept_uploaded = 0
    with engine_for(tid).begin() as conn:
        if not IS_SQLITE:
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        keep = [dict(r) for r in conn.execute(settings.select()).mappings() if _protected(r["key"])]
        if data.get("tenant") == tid:
            kept_uploaded = _keep_uploaded(conn, prepared.get("clips") or [])
        if not is_super:  # a file must never be able to add an approved helper (helper approval is a switch)
            wk = next(t for t in tables() if t.name == "workers")
            prepared["workers"] = [dict(r) for r in conn.execute(wk.select()).mappings()]
        for t in reversed(tables()):
            conn.execute(t.delete())
        for t in tables():
            rows = prepared[t.name]
            if t.name == "settings":
                rows = [r for r in rows if not _protected(r["key"])] + keep
            for i in range(0, len(rows), 1000):
                conn.execute(t.insert(), rows[i:i + 1000])
        _reset_sequences(conn, tid)
    return kept_uploaded


def _keep_uploaded(conn, rows: list) -> int:
    """A clip that went up to YouTube AFTER the backup was made is still waiting in the backup. Keep it marked as
    uploaded (same clip number, same video part), so it is not uploaded a second time."""
    from .db import Clip

    c = Clip.__table__
    now = {r.id: r for r in conn.execute(c.select().where(c.c.youtube_id.isnot(None)))}
    n = 0
    for row in rows:
        cur = now.get(row["id"])
        if cur and cur.archived_at and not row.get("archived_at") and row.get("youtube_id") in (None, cur.youtube_id) \
                and (cur.project_id, cur.start, cur.end) == (row.get("project_id"), row.get("start"), row.get("end")):
            # saved on the PC after the backup was made (its server file may be gone already): keep that record
            row.update(archived_at=cur.archived_at, archived_sha=cur.archived_sha, archived_src_sha=cur.archived_src_sha)
        if cur and not row.get("youtube_id") and (cur.project_id, cur.start, cur.end) == (row.get("project_id"), row.get("start"), row.get("end")):
            row.update(youtube_id=cur.youtube_id, status=cur.status, attempted_at=cur.attempted_at, publish_at=cur.publish_at,
                       error=None)
            n += 1
    return n


def _faces(faces: dict) -> dict:
    out = {}
    for name, b64 in faces.items():
        if not FACE_RE.match(name):
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError):
            raise BackupError("The file is damaged (a face photo cannot be read).") from None
        if len(raw) > MAX_FACE or raw[:8] != b"\x89PNG\r\n\x1a\n":
            continue
        out[name] = raw
    return out


def _put_faces(faces: dict):
    d = DATA_DIR / "faces"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.png"):
        if f.name not in faces:
            f.unlink(missing_ok=True)
    for name, raw in faces.items():
        (d / name).write_bytes(raw)


def _after(tid: int):
    """Caches that remember the old data."""
    try:
        from . import acl

        acl.invalidate()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .scheduler import recover

        recover()  # clips that were being made or uploaded when the backup was taken carry on
    except Exception as e:  # noqa: BLE001
        log.warning("after restore: %s", str(e)[:200])
