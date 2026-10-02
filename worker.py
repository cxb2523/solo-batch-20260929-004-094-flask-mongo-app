"""Inspection worker that owns the EXPIRE and CLEANUP transitions.

It supports two run styles:

* :meth:`InspectionWorker.run_once` - a single manual sweep (``run.py
  --inspect``).
* :meth:`InspectionWorker.schedule` - a recurring timer-driven sweep
  (``run.py --inspect --interval 60``).

Every sweep strips expired tokens and redeemed residue from user records and
writes a self-contained HTML page under ``reports/`` that opens on a double
click (no server or network needed).
"""

import datetime
import html
import os
import threading

REPORTS_DIR = "reports"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"

# The three trade-offs, stated as chosen option vs rejected option with the
# consequence. They are rendered into every report under reports/.
DESIGN_DECISIONS = [
    {
        "title": "1. Token storage: embedded on the user record vs separate collection",
        "choice": "Embed the signed token plus an expiry stamp directly on the "
                  "user document (confirmation_token / token_issued_at / "
                  "token_expires_at / token_redeemed_at).",
        "rejected": "A standalone tokens collection with its own lifecycle.",
        "consequences": [
            "Resend rotates the old link: reissuing overwrites the embedded "
            "token, so links from older emails can no longer match any record.",
            "Concurrent double clicks are idempotent while the redeemed residue "
            "(token_redeemed_at) is retained: the second click resolves to "
            "already_active instead of activating twice.",
            "There are no orphan token rows to reap across collections; the "
            "worker sweep clearing the four fields is the only reaper.",
        ],
    },
    {
        "title": "2. Expiry comparison: UTC strict greater-than vs local / greater-than-or-equal",
        "choice": "Compare now > token_expires_at using UTC epoch seconds, "
                  "strict greater-than.",
        "rejected": "Local server time with >=, or the serializer's own clock.",
        "consequences": [
            "UTC avoids a deployed server drifting between timezones/DST "
            "versus the issued stamp.",
            "At the exact boundary second (now == token_expires_at) the link "
            "is still valid; >= would expire a link the very instant it is "
            "issued when stamps coincide, while a naive/never-set local "
            "comparison can make a link live forever.",
        ],
    },
    {
        "title": "3. Confirmation failure: inline branch vs redirect to login",
        "choice": "Default renders confirm.html inline with an explicit reason "
                  "branch (invalid / expired / already_active / user_not_found); "
                  "a redirect-to-login disposition is also implemented and is "
                  "counted separately.",
        "rejected": "Always redirecting every failure to /login with one "
                    "generic error.",
        "consequences": [
            "The two paths are distinguishable in the report: inline_failures "
            "vs redirect_failures counters.",
            "The three failure classes keep their own counters, so 'link "
            "invalid', 'account already active' and 'user does not exist' are "
            "never merged into one opaque bucket.",
        ],
    },
]


class SweepResult:
    def __init__(self, expired_cleaned=0, redeemed_cleaned=0, actions=None):
        self.expired_cleaned = expired_cleaned
        self.redeemed_cleaned = redeemed_cleaned
        self.actions = actions if actions is not None else []

    @property
    def total_cleaned(self):
        return self.expired_cleaned + self.redeemed_cleaned


class InspectionWorker:
    def __init__(self, store, metrics=None, clock=None, reports_dir=REPORTS_DIR):
        self.store = store
        self.metrics = metrics
        self.clock = clock or _utc_now
        self.reports_dir = reports_dir

    def sweep(self):
        """EXPIRE scan + CLEANUP migration. One pass over all users."""
        result = SweepResult()
        now = self.clock()
        for user in self.store.all_users():
            if user.token_redeemed_at:
                # Redeemed residue left behind after the single successful
                # activation; safe to strip now.
                self.store.clear_token_fields(user.id)
                result.redeemed_cleaned += 1
                result.actions.append((user.email, "redeemed_residue"))
            elif user.confirmation_token and now > user.token_expires_at:
                # Strict greater-than UTC comparison (see decision 2).
                self.store.clear_token_fields(user.id)
                result.expired_cleaned += 1
                result.actions.append((user.email, "expired"))
        if self.metrics is not None:
            self.metrics.record_sweep(result.expired_cleaned,
                                      result.redeemed_cleaned)
        return result

    def run_once(self, trigger=TRIGGER_MANUAL):
        result = self.sweep()
        return self.write_report(result, trigger)

    def schedule(self, interval_seconds, stop_event=None):
        """Timer-driven sweeps until ``stop_event`` is set. Used by --interval."""
        stop_event = stop_event if stop_event is not None else threading.Event()
        produced = []
        while not stop_event.is_set():
            produced.append(self.run_once(TRIGGER_SCHEDULED))
            if stop_event.wait(interval_seconds):
                break
        return produced

    def write_report(self, result, trigger):
        os.makedirs(self.reports_dir, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc)
        path = os.path.join(
            self.reports_dir,
            "inspection-{0}-{1:06d}.html".format(
                stamp.strftime("%Y%m%d-%H%M%S"), stamp.microsecond))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render_report(result, trigger, stamp, self.metrics))
        # Keep a stable, always-current pointer as well.
        latest = os.path.join(self.reports_dir, "latest.html")
        with open(latest, "w", encoding="utf-8") as handle:
            handle.write(render_report(result, trigger, stamp, self.metrics))
        write_decisions_markdown(self.reports_dir)
        return path


def render_report(result, trigger, stamp, metrics=None):
    snap = metrics.snapshot() if metrics is not None else None
    outcome_labels = [
        ("activated", "Activated (success)"),
        ("already_active", "Account already active"),
        ("expired", "Link expired"),
        ("invalid", "Link invalid / rotated"),
        ("user_not_found", "User does not exist"),
    ]

    rows = []
    if snap:
        for key, label in outcome_labels:
            rows.append(
                "<tr><td>{0}</td><td>{1}</td></tr>".format(
                    html.escape(label), snap["attempts"].get(key, 0)))
    outcome_rows = "\n".join(rows)

    action_rows = "\n".join(
        "<tr><td>{0}</td><td>{1}</td></tr>".format(html.escape(email),
                                                   html.escape(reason))
        for email, reason in result.actions) or \
        "<tr><td colspan='2'>No records required cleanup this run.</td></tr>"

    decisions = []
    for decision in DESIGN_DECISIONS:
        consequences = "".join(
            "<li>{0}</li>".format(html.escape(item))
            for item in decision["consequences"])
        decisions.append(
            "<section class='card'>"
            "<h3>{title}</h3>"
            "<p><strong>Chosen:</strong> {choice}</p>"
            "<p><strong>Rejected:</strong> {rejected}</p>"
            "<ul>{consequences}</ul>"
            "</section>".format(
                title=html.escape(decision["title"]),
                choice=html.escape(decision["choice"]),
                rejected=html.escape(decision["rejected"]),
                consequences=consequences))
    decision_html = "\n".join(decisions)

    inline_failures = snap["inline_failures"] if snap else "-"
    redirect_failures = snap["redirect_failures"] if snap else "-"
    total_attempts = snap["total_attempts"] if snap else "-"
    runs = snap["runs"] if snap else 1

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Token inspection report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 2rem; color: #1f2933; background: #f7f9fb; }}
h1 {{ margin-bottom: 0; }}
.meta {{ color: #52606d; margin-top: .25rem; }}
.grid {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1.5rem 0; }}
.stat {{ background: #fff; border: 1px solid #e4e7eb; border-radius: 8px; padding: 1rem 1.25rem; min-width: 150px; }}
.stat .num {{ font-size: 1.8rem; font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; }}
th, td {{ border: 1px solid #e4e7eb; padding: .5rem .75rem; text-align: left; }}
th {{ background: #edf2f7; }}
.card {{ background: #fff; border: 1px solid #e4e7eb; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }}
.pill {{ display:inline-block; padding:.15rem .6rem; border-radius:999px; background:#e3f2fd; }}
</style>
</head>
<body>
<h1>Token lifecycle inspection report</h1>
<p class="meta">Generated {stamp} UTC &middot; trigger: <span class="pill">{trigger}</span> &middot; worker runs (cumulative): {runs}</p>

<h2>This sweep</h2>
<div class="grid">
  <div class="stat"><div class="num">{expired}</div><div>expired tokens swept</div></div>
  <div class="stat"><div class="num">{redeemed}</div><div>redeemed residue swept</div></div>
  <div class="stat"><div class="num">{total}</div><div>total cleaned</div></div>
</div>

<h2>Confirmation attempts (cumulative)</h2>
<div class="grid">
  <div class="stat"><div class="num">{total_attempts}</div><div>total attempts</div></div>
  <div class="stat"><div class="num">{inline_failures}</div><div>failures shown inline (confirm.html)</div></div>
  <div class="stat"><div class="num">{redirect_failures}</div><div>failures redirected to login</div></div>
</div>
<table>
<thead><tr><th>Outcome</th><th>Count</th></tr></thead>
<tbody>
{outcome_rows}
</tbody>
</table>

<h2>Records cleaned in this run</h2>
<table>
<thead><tr><th>User</th><th>Reason</th></tr></thead>
<tbody>
{action_rows}
</tbody>
</table>

<h2>Design trade-offs</h2>
{decision_html}
</body>
</html>
""".format(
        stamp=stamp.strftime("%Y-%m-%d %H:%M:%S"),
        trigger=html.escape(trigger),
        runs=runs,
        expired=result.expired_cleaned,
        redeemed=result.redeemed_cleaned,
        total=result.total_cleaned,
        total_attempts=total_attempts,
        inline_failures=inline_failures,
        redirect_failures=redirect_failures,
        outcome_rows=outcome_rows,
        action_rows=action_rows,
        decision_html=decision_html)


def write_decisions_markdown(reports_dir):
    os.makedirs(reports_dir, exist_ok=True)
    lines = ["# Token lifecycle - design trade-offs", ""]
    for decision in DESIGN_DECISIONS:
        lines.append("## " + decision["title"])
        lines.append("- Chosen: " + decision["choice"])
        lines.append("- Rejected: " + decision["rejected"])
        lines.append("- Consequences:")
        for item in decision["consequences"]:
            lines.append("  - " + item)
        lines.append("")
    path = os.path.join(reports_dir, "design-decisions.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
