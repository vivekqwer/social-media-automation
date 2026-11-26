"""Free trial and how new accounts start (Super Admin settings, kept in the owner's copy).

* "Approve new accounts by hand" (default, as before) or "Start the trial at once" (the account is approved as soon
  as it registers). Nothing changes until the Super Admin picks the second one.
* Free trial (on by default, 7 days, on the plan new accounts start on unless another plan is picked): when an account
  is approved for the first time it is put on the trial plan with an end date. No card is asked for.
  The end uses exactly the paid-plan machinery (app/payments.py): Account.plan_expires / paid_plan / limit_before_pay,
  ended by payments.expire_due (daily) -> back to the free plan, nothing deleted. Account.trial marks it as a trial
  (reminder 2 days before instead of 3, its own words). Paying for a plan ends the trial at once (_apply_paid).
* A trial is only started when the trial plan is not the plan an account falls back to anyway (otherwise there is
  nothing to end, and the customer would get a pointless "your trial ended" message).
* The Super Admin can extend or end a trial in the customer's Access panel.
"""
import json
from datetime import timedelta

from . import tenant
from .accounts import Account, CoreSession
from .db import get_setting, set_setting, utcnow

KEY = "signup"
DEFAULT = {"approve": "hand", "trial_on": True, "trial_days": 7, "trial_plan": ""}
WARN_DAYS = 2
MAX_DAYS = 90


def config() -> dict:
    with tenant.use(tenant.OWNER):
        raw = get_setting(KEY)
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    out = dict(DEFAULT)
    if isinstance(saved, dict):
        out.update({k: saved[k] for k in DEFAULT if k in saved})
    out["approve"] = "auto" if out.get("approve") == "auto" else "hand"
    out["trial_on"] = bool(out.get("trial_on"))
    try:
        out["trial_days"] = max(1, min(MAX_DAYS, int(out.get("trial_days") or 7)))
    except (TypeError, ValueError):
        out["trial_days"] = 7
    out["trial_plan"] = str(out.get("trial_plan") or "")[:40]
    return out


def save(approve: str | None = None, trial_on: bool | None = None, trial_days: int | None = None,
         trial_plan: str | None = None) -> dict:
    from . import acl

    cfg = config()
    if approve is not None:
        if approve not in ("hand", "auto"):
            raise ValueError("Choose “by hand” or “start the trial at once”.")
        cfg["approve"] = approve
    if trial_on is not None:
        cfg["trial_on"] = bool(trial_on)
    if trial_days is not None:
        if not 1 <= int(trial_days) <= MAX_DAYS:
            raise ValueError(f"A trial lasts 1 to {MAX_DAYS} days.")
        cfg["trial_days"] = int(trial_days)
    if trial_plan is not None:
        name = acl._plan_name(acl.config(), trial_plan) if trial_plan else ""
        if trial_plan and name is None:
            raise ValueError("There is no plan with this name.")
        cfg["trial_plan"] = name or ""
    with tenant.use(tenant.OWNER):
        set_setting(KEY, json.dumps(cfg))
    return public()


def plan_name() -> str:
    """The plan a trial runs on: the one picked, else the plan new accounts start on ('' = no usable plan)."""
    from . import acl

    cfg = config()
    name = acl._plan_name(acl.config(), cfg["trial_plan"]) if cfg["trial_plan"] else None
    return name or acl.new_account_plan() or ""


def effective() -> str:
    """The trial plan when a trial would really give something ('' = no trial is started)."""
    from .payments import _free_plan

    cfg = config()
    plan = plan_name()
    if not cfg["trial_on"] or not plan or plan.lower() == (_free_plan() or "").lower():
        return ""
    return plan


def public() -> dict:
    """For the Super Admin's card."""
    cfg = config()
    plan = plan_name()
    eff = effective()
    note = "" if eff or not cfg["trial_on"] else (
        "No trial is started: the trial plan is the same plan accounts fall back to. Pick another plan for the trial."
        if plan else "No trial is started: there is no plan yet. Make a plan in Plans & defaults and pick it here.")
    return {**cfg, "plan": plan, "active": bool(eff), "note": note}


def landing() -> dict:
    """What the public home page may show (no plan details beyond this)."""
    cfg = config()
    return {"trial_days": cfg["trial_days"] if effective() else 0, "instant": cfg["approve"] == "auto"}


def start(account_id: int) -> dict | None:
    """Put a newly approved account on the trial plan with an end date. Never raises (a registration must not fail)."""
    try:
        from . import acl

        plan = effective()
        if not plan:
            return None
        days = config()["trial_days"]
        with CoreSession() as s:
            a = s.get(Account, int(account_id))
            if not a or a.plan_expires or a.paid_plan:
                return None  # already on a paid / timed plan
            before = a.channel_limit or 1
        acl.apply_plan(int(account_id), plan, keep_restrictions=True)
        ends = utcnow() + timedelta(days=days)
        with CoreSession() as s:
            a = s.get(Account, int(account_id))
            a.plan_expires, a.expiry_warned, a.paid_plan, a.limit_before_pay, a.trial = ends, None, plan, before, True
            s.commit()
        acl.invalidate()
        acl.record(int(account_id), "system", "payment", f"Free trial started — plan “{plan}” for {days} days, until {ends:%d %b %Y}",
                   a.tenant_id)
        return {"plan": plan, "ends": ends.isoformat() + "Z"}
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("trial could not be started for account %s", account_id)
        return None


def extend(account_id: int, days: int) -> dict:
    if not 1 <= int(days) <= MAX_DAYS:
        raise ValueError(f"Add 1 to {MAX_DAYS} days.")
    from . import acl

    now = utcnow()
    with CoreSession() as s:
        a = s.get(Account, int(account_id))
        if not a:
            raise KeyError(account_id)
        if not a.trial or not a.plan_expires:
            raise ValueError("This account is not on a free trial.")
        a.plan_expires = max(a.plan_expires, now) + timedelta(days=int(days))
        a.expiry_warned = None
        s.commit()
        ends, tid = a.plan_expires, a.tenant_id
    acl.invalidate()
    acl.record(int(account_id), "super", "payment", f"Free trial extended by {int(days)} days, until {ends:%d %b %Y}", tid)
    return {"trial": True, "ends": ends.isoformat() + "Z"}


def end(account_id: int) -> dict:
    """End the trial now: the same end as a paid plan (back to the free plan, nothing deleted)."""
    from . import acl, payments

    with CoreSession() as s:
        a = s.get(Account, int(account_id))
        if not a:
            raise KeyError(account_id)
        if not a.trial or not a.plan_expires:
            raise ValueError("This account is not on a free trial.")
        a.plan_expires = utcnow() - timedelta(seconds=1)
        s.commit()
        tid = a.tenant_id
    acl.record(int(account_id), "super", "payment", "Ended the free trial", tid)
    with tenant.use(tenant.OWNER):
        payments.expire_due()
    return {"trial": False}


def of(a: Account | None) -> dict | None:
    """{"ends", "days_left"} while the account is on a trial."""
    if not a or not a.trial or not a.plan_expires:
        return None
    left = (a.plan_expires - utcnow()).total_seconds() / 86400
    return {"ends": a.plan_expires.isoformat() + "Z", "days_left": max(0, int(left + 0.999)), "plan": a.paid_plan}
