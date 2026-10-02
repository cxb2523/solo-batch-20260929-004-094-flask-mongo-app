"""Application entry point: Flask web app and token-lifecycle worker.

Run modes:

    python run.py                       # web server
    python run.py worker                # one manual sweep, writes reports/
    python run.py worker --interval 60  # scheduled sweep loop (seconds)

Both the data store and the audit log are injectable; with no MongoDB
configuration the app runs fully offline against an in-memory fake store.
"""

import argparse
import configparser
import os
import time
from urllib.parse import urljoin, urlparse

from flask import Flask, abort, redirect, render_template, request, url_for
from flask_bcrypt import Bcrypt
from flask_login import (
    AnonymousUserMixin,
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import events
import report as report_module
import worker
from email_utility import send_message_email, send_registration_email
from message import Message
from note import Note
from store import FakeDataStore, default_store
from user import User


def read_secret_key():
    parser = configparser.ConfigParser()
    parser.read("configuration.ini")
    defaults = parser.defaults()
    return os.environ.get(
        "SECRET_KEY", defaults.get("SECRET_KEY", "development-secret-key"))


def create_app(store=None, event_log=None, config=None):
    app = Flask(__name__)
    config = dict(config or {})
    app.secret_key = config.get("SECRET_KEY", read_secret_key())
    app.config["WTF_CSRF_ENABLED"] = config.get("WTF_CSRF_ENABLED", True)
    app.config["TESTING"] = config.get("TESTING", False)
    app.config["PREFERRED_URL_SCHEME"] = "http"

    app.extensions["data_store"] = (
        store if store is not None else default_store())
    app.extensions["event_log"] = (
        event_log if event_log is not None else events.InMemoryEventLog())
    failure_mode = config.get(
        "CONFIRM_FAILURE_MODE",
        os.environ.get("CONFIRM_FAILURE_MODE", worker.INLINE))
    if failure_mode not in (worker.INLINE, worker.REDIRECT):
        failure_mode = worker.INLINE
    app.config["CONFIRM_FAILURE_MODE"] = failure_mode

    bc = Bcrypt(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.anonymous_user = AnonymousUserMixin
    login_manager.login_view = "login"

    if app.config["WTF_CSRF_ENABLED"]:
        from flask_wtf.csrf import CSRFProtect
        CSRFProtect(app)
    else:
        app.jinja_env.globals["csrf_token"] = lambda: ""

    def is_safe_url(target):
        ref_url = urlparse(request.host_url)
        test_url = urlparse(urljoin(request.host_url, target))
        return test_url.scheme in ("http", "https") and \
            ref_url.netloc == test_url.netloc

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if current_user.is_authenticated:
                return redirect(url_for("index"))
            return render_template(
                "login.html",
                error=request.args.get("error"),
                confirm_error=request.args.get("confirm_error"))
        user_data = store.find_one(
            "users", {"email": request.form["email"]}, {"_id": 0})
        if user_data and bc.check_password_hash(
                user_data["password"], request.form["pass"]):
            user = User.make_from_dict(user_data)
            login_user(user)
            next_url = request.args.get("next")
            if not is_safe_url(next_url):
                return abort(400)
            return redirect(next_url or url_for("profile"))
        return redirect(url_for("login", error=1))

    @app.route("/register", methods=["POST", "GET"])
    def register():
        if request.method == "GET":
            return render_template(
                "register.html", error=request.args.get("error"))

        email = request.form["email"].strip()
        title = request.form["title"].strip()
        first_name = request.form["first_name"].strip()
        last_name = request.form["last_name"].strip()
        password = request.form["pass"].strip()

        if store.find_one("users", {"email": email}, {"_id": 0}) is not None:
            return redirect(url_for("register", error=1))

        logout_user()
        new_user = User(title, first_name, last_name, email)
        user_data = new_user.dict()
        user_data["password"] = bc.generate_password_hash(
            password).decode("utf-8")

        if not store.insert_one("users", user_data):
            return redirect(url_for("register", error=2))

        # Issue -> embedded token + UTC expiry stamp on the user record.
        worker.issue_token(store, new_user, event_log=event_log)
        login_user(new_user)
        try:
            send_registration_email(new_user)
        except Exception:
            # Offline / no SMTP configured: registration still succeeds.
            pass
        return redirect(url_for("profile"))

    @app.route("/confirm/<token>", methods=["GET"])
    def confirm_email(token):
        logout_user()
        outcome, _user = worker.attempt_confirmation(
            store, token, path=failure_mode, event_log=event_log)
        if outcome == worker.SUCCESS:
            return render_template("confirm.html", outcome=outcome)
        if failure_mode == worker.REDIRECT:
            return redirect(url_for("login", confirm_error=outcome))
        return render_template("confirm.html", outcome=outcome), 200

    @app.route("/verify", methods=["POST"])
    @login_required
    def send_verification_email():
        if current_user.verified:
            return "Your email address is already verified"
        # Resend rotates the embedded token and invalidates the old link.
        worker.issue_token(store, current_user, event_log=event_log)
        try:
            send_registration_email(current_user)
        except Exception:
            pass
        return "Verification email sent"

    @app.route("/profile", methods=["GET"])
    @login_required
    def profile():
        notes = store.find(
            "notes", {"user_id": current_user.id, "deleted": False})
        if hasattr(notes, "sort"):
            notes = notes.sort("timestamp", -1)
        return render_template("profile.html", notes=notes,
                               title=current_user.title)

    @app.route("/messages", methods=["GET"])
    @login_required
    def messages():
        all_users = store.find(
            "users", {"id": {"$ne": current_user.id}}, {"_id": 0})
        inbox = store.find(
            "messages", {"to_id": current_user.id, "deleted": False})
        if hasattr(inbox, "sort"):
            inbox = inbox.sort("timestamp", -1)
        sent = store.find(
            "messages",
            {"from_id": current_user.id, "deleted": False,
             "hidden_for_sender": False})
        if hasattr(sent, "sort"):
            sent = sent.sort("timestamp", -1)
        return render_template(
            "messages.html", users=all_users,
            inbox_messages=inbox, sent_messages=sent)

    @app.route("/logout", methods=["GET"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    @app.route("/add_note", methods=["POST"])
    @login_required
    def add_note():
        note = Note(request.form.get("title"), request.form.get("body"),
                    current_user.id, current_user.display_name())
        if store.insert_one("notes", note.dict()):
            return "Success! Note added: " + note.title
        return "Error! Could not add note"

    @app.route("/delete_note", methods=["POST"])
    @login_required
    def delete_note():
        if store.update_one("notes", {"id": request.form.get("note_id")},
                            {"$set": {"deleted": True}}):
            return "Success! Note deleted"
        return "Error! Could not delete note"

    @app.route("/send_message", methods=["POST"])
    @login_required
    def send_message():
        to_user_dict = store.find_one(
            "users", {"id": request.form.get("user")})
        if not to_user_dict:
            return "Error! Recipient not found"
        to_user = User.make_from_dict(to_user_dict)
        message = Message(
            request.form.get("title"), request.form.get("body"),
            current_user.id, current_user.display_name(),
            to_user.id, to_user.display_name())
        if store.insert_one("messages", message.dict()):
            try:
                send_message_email(from_user=current_user, to_user=to_user,
                                   message=message)
            except Exception:
                pass
            return "Success! Message sent to " + to_user.display_name()
        return "Error! Could not send message"

    @app.route("/delete_message", methods=["POST"])
    @login_required
    def delete_message():
        if store.update_one("messages", {"id": request.form.get("message_id")},
                            {"$set": {"deleted": True}}):
            return "Success! Message deleted"
        return "Error! Could not delete message"

    @app.route("/hide_sent_message", methods=["POST"])
    @login_required
    def hide_sent_message():
        if store.update_one("messages", {"id": request.form.get("message_id")},
                            {"$set": {"hidden_for_sender": True}}):
            return "Success! Message hidden from sender"
        return "Error! Could not hide message"

    @app.route("/change_name", methods=["POST"])
    @login_required
    def change_name():
        fields = {
            "title": request.form["title"].strip(),
            "first_name": request.form["first_name"].strip(),
            "last_name": request.form["last_name"].strip()}
        if store.update_one("users", {"email": current_user.email},
                            {"$set": fields}):
            return "User name updated successfully"
        return "Error! Could not update user name"

    @app.route("/delete_account", methods=["POST"])
    @login_required
    def delete_account():
        user_id = current_user.id
        user_deleted = bool(store.delete_one("users", {"id": user_id}))
        if user_deleted:
            logout_user()
        notes_deleted = bool(store.delete_many("notes", {"user_id": user_id}))
        messages_deleted = bool(store.delete_many(
            "messages",
            {"$or": [{"from_id": user_id}, {"to_id": user_id}]}))
        return {"user_deleted": user_deleted, "notes_deleted": notes_deleted,
                "messages_deleted": messages_deleted}

    @login_manager.user_loader
    def load_user(userid):
        user = store.find_one("users", {"id": userid}, {"_id": 0})
        if user:
            return User.make_from_dict(user)
        return None

    return app


def run_worker_once(store, event_log, reports_dir, failure_mode=worker.INLINE):
    result = worker.sweep(store, event_log=event_log)
    path = report_module.write_report(
        reports_dir, result, event_log, failure_mode=failure_mode)
    return result, path


def seed_demo_data(store, event_log):
    """Seed an offline demo store and drive the confirmation state machine."""
    from datetime import timedelta

    from verification import utcnow

    moment = utcnow()

    # Active user carrying stale token residue.
    active = User("", "Ada", "Lovelace", "ada@example.com", verified=True)
    active.confirmed_at = moment - timedelta(days=1)
    record = active.dict()
    record["confirm_token"] = "stale-residue-token"
    record["confirm_expires_at"] = moment - timedelta(hours=2)
    store.insert_one("users", record)

    # User whose token is expired and was never redeemed.
    stale = User("", "Grace", "Hopper", "grace@example.com")
    store.insert_one("users", stale.dict())
    expired_token, _expiry, _ = worker.issue_token(
        store, stale, ttl_seconds=-3600, event_log=event_log, now=moment)

    # Fresh user: first click succeeds, the double-click is idempotent.
    fresh = User("", "Alan", "Turing", "alan@example.com")
    store.insert_one("users", fresh.dict())
    fresh_token, _exp, _ = worker.issue_token(
        store, fresh, event_log=event_log, now=moment)
    worker.attempt_confirmation(
        store, fresh_token, path=worker.INLINE, event_log=event_log, now=moment)
    worker.attempt_confirmation(
        store, fresh_token, path=worker.INLINE, event_log=event_log, now=moment)

    # Failure reasons exercised on both rendering paths.
    worker.attempt_confirmation(
        store, "garbage.token.value", path=worker.INLINE,
        event_log=event_log, now=moment)
    worker.attempt_confirmation(
        store, "garbage.token.value", path=worker.REDIRECT,
        event_log=event_log, now=moment)
    worker.attempt_confirmation(
        store, expired_token, path=worker.INLINE,
        event_log=event_log, now=moment)
    worker.attempt_confirmation(
        store, expired_token, path=worker.REDIRECT,
        event_log=event_log, now=moment)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Flask token lifecycle app")
    sub = parser.add_subparsers(dest="command")
    worker_parser = sub.add_parser("worker", help="run the sweep worker")
    worker_parser.add_argument(
        "--interval", type=int, default=0,
        help="scheduled mode: seconds between sweeps (0 = run once)")
    worker_parser.add_argument(
        "--reports-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "reports"))
    worker_parser.add_argument(
        "--seed-demo", action="store_true",
        help="seed a throwaway in-memory store with demo activity")
    args = parser.parse_args(argv)

    if args.command == "worker":
        failure_mode = os.environ.get("CONFIRM_FAILURE_MODE", worker.INLINE)
        if failure_mode not in (worker.INLINE, worker.REDIRECT):
            failure_mode = worker.INLINE
        os.makedirs(args.reports_dir, exist_ok=True)
        if args.seed_demo:
            store = FakeDataStore()
            event_log = events.InMemoryEventLog()
            seed_demo_data(store, event_log)
        else:
            store = default_store()
            event_log = events.JsonlEventLog(
                os.path.join(args.reports_dir, "events.jsonl"))

        def cycle():
            result, path = run_worker_once(
                store, event_log, args.reports_dir, failure_mode)
            print(f"sweep: scanned={result.users_scanned} "
                  f"expired={result.expired_tokens} "
                  f"residue={result.confirmed_residue} report={path}")

        if args.interval and args.interval > 0:
            while True:
                cycle()
                time.sleep(args.interval)
        else:
            cycle()
        return 0

    app = create_app()
    if os.environ.get("APP_LOCATION") == "heroku":
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        app.run(host="localhost", port=8080, debug=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
