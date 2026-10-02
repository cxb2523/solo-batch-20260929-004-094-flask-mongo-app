"""Application entry point and inspection worker driver.

Run modes
---------
``python run.py``                            serve the Flask app
``python run.py --inspect``                  one manual sweep + HTML report
``python run.py --inspect --interval 60``    recurring timed sweeps

The data store is injectable; the default is the offline in-memory
:class:`store.FakeStore`, so the whole flow runs with no MongoDB or network.
"""

import argparse
import configparser
import os
import threading
import time
from urllib.parse import urljoin, urlparse

from flask import (Flask, abort, redirect, render_template, request,
                   url_for)
from flask_bcrypt import Bcrypt
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_wtf.csrf import CSRFProtect

from email_utility import send_registration_email
from store import FakeStore
from user import Anonymous, User
from verification import (ACTIVATED, ALREADY_ACTIVE, DISPOSITION_INLINE,
                           DISPOSITION_REDIRECT, EXPIRED, INVALID,
                           USER_NOT_FOUND, Metrics, TokenService)
from worker import (InspectionWorker, TRIGGER_MANUAL,
                    write_decisions_markdown)


FAILURE_MESSAGES = {
    INVALID: "The verification link is invalid or has been replaced by a "
             "newer one.",
    EXPIRED: "The verification link has expired. Please request a new one.",
    ALREADY_ACTIVE: "This account has already been activated.",
    USER_NOT_FOUND: "No account exists for this verification link.",
}


def create_app(store=None, clock=None, token_ttl=3600,
               failure_disposition=DISPOSITION_INLINE):
    """Build the Flask app around an injectable store."""
    app = Flask(__name__)

    config = configparser.ConfigParser()
    config.read(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'configuration.ini'))
    defaults = config.defaults()
    app.secret_key = os.environ.get(
        'SECRET_KEY', defaults.get('SECRET_KEY', 'change-me'))
    app.config['PREFERRED_URL_SCHEME'] = "https"
    app.config['TESTING'] = bool(os.environ.get('FLASK_TESTING'))

    store = store if store is not None else FakeStore()
    metrics = Metrics()
    token_service = TokenService(store, clock=clock, ttl=token_ttl,
                                 metrics=metrics)

    app.extensions['user_store'] = store
    app.extensions['token_service'] = token_service
    app.extensions['metrics'] = metrics
    app.extensions['failure_disposition'] = failure_disposition

    bc = Bcrypt(app)
    csrf = CSRFProtect()
    csrf.init_app(app)
    if app.config['TESTING']:
        app.config['WTF_CSRF_ENABLED'] = False

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.anonymous_user = Anonymous
    login_manager.login_view = "login"

    # Talisman is optional and skipped offline/in tests so HTTPS is not forced.
    if not app.config['TESTING']:
        try:
            from flask_talisman import Talisman
            Talisman(app, content_security_policy={
                'default-src': ["'self'",
                                'https://stackpath.bootstrapcdn.com',
                                'https://pro.fontawesome.com',
                                'https://code.jquery.com',
                                'https://cdnjs.cloudflare.com']})
        except Exception:
            pass

    @login_manager.user_loader
    def load_user(user_id):
        return store.find_user_by_id(user_id)

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            if current_user.is_authenticated:
                return redirect(url_for('index'))
            reason = request.args.get("reason")
            return render_template(
                'login.html',
                error=request.args.get("error"),
                reason=reason,
                reason_message=FAILURE_MESSAGES.get(reason))

        user = store.find_user_by_email(request.form['email'].strip())
        if user and user.password and bc.check_password_hash(
                user.password, request.form['pass']):
            login_user(user)
            target = request.args.get('next')
            if not is_safe_url(target):
                abort(400)
            return redirect(target or url_for('profile'))
        return redirect(url_for('login', error=1))

    @app.route('/register', methods=['POST', 'GET'])
    def register():
        if request.method == 'POST':
            email = request.form['email'].strip()
            title = request.form['title'].strip()
            first_name = request.form['first_name'].strip()
            last_name = request.form['last_name'].strip()
            password = request.form['pass'].strip()

            if store.find_user_by_email(email) is not None:
                return redirect(url_for('register', error=1))

            logout_user()
            new_user = User(title, first_name, last_name, email)
            record = new_user.dict()
            record['password'] = bc.generate_password_hash(
                password).decode('utf-8')
            store.insert_user(record)
            # ISSUE transition: token embedded on the user record + UTC stamp.
            token = token_service.issue(new_user)
            login_user(new_user)
            try:
                send_registration_email(new_user, token=token)
            except Exception:
                # SMTP is unavailable offline; activation still works.
                pass
            return redirect(url_for('profile'))

        return render_template('register.html',
                               error=request.args.get("error"))

    @app.route('/confirm/<token>', methods=['GET'])
    def confirm_email(token):
        logout_user()
        disposition = app.extensions['failure_disposition']
        outcome = token_service.redeem(token, disposition=disposition)

        if outcome == ACTIVATED:
            return render_template('confirm.html', success=True, outcome=outcome)

        if disposition == DISPOSITION_REDIRECT:
            return redirect(url_for('login', reason=outcome))

        return render_template(
            'confirm.html', success=False, outcome=outcome,
            error_message=FAILURE_MESSAGES.get(outcome))

    @app.route('/verify', methods=['POST'])
    @login_required
    def send_verification_email():
        if not current_user.verified:
            # Resend rotates: the old link is overwritten and stops matching.
            token = token_service.issue(current_user)
            try:
                send_registration_email(current_user, token=token)
            except Exception:
                pass
            return "Verification email sent"
        return "Your email address is already verified"

    @app.route('/profile', methods=['GET'])
    @login_required
    def profile():
        return render_template('profile.html', notes=[],
                               title=current_user.title)

    @app.route('/logout', methods=['GET'])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('index'))

    return app


def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and \
        ref_url.netloc == test_url.netloc


def build_worker(app=None, reports_dir="reports"):
    app = app or create_app()
    return InspectionWorker(
        app.extensions['user_store'],
        metrics=app.extensions['metrics'],
        reports_dir=reports_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Flask token lifecycle app")
    parser.add_argument('--inspect', action='store_true',
                        help="run the inspection worker instead of the server")
    parser.add_argument('--interval', type=float, default=0,
                        help="scheduled mode: seconds between sweeps")
    parser.add_argument('--max-runs', type=int, default=0,
                        help="scheduled mode: stop after this many sweeps")
    parser.add_argument('--reports-dir', default="reports")
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args(argv)

    if args.inspect:
        app = create_app()
        worker = build_worker(app, reports_dir=args.reports_dir)
        write_decisions_markdown(args.reports_dir)
        if args.interval > 0:
            return _run_scheduled(worker, args)
        path = worker.run_once(TRIGGER_MANUAL)
        print("Inspection report written to {0}".format(path))
        return 0

    app = create_app()
    if os.environ.get('APP_LOCATION') == 'heroku':
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        app.run(host=args.host, port=args.port, debug=True)
    return 0


def _run_scheduled(worker, args):
    from worker import TRIGGER_SCHEDULED
    stop_event = threading.Event()

    def runner():
        runs = 0
        while not stop_event.is_set():
            path = worker.run_once(TRIGGER_SCHEDULED)
            print("Scheduled inspection report: {0}".format(path))
            runs += 1
            if args.max_runs and runs >= args.max_runs:
                stop_event.set()
                return
            if stop_event.wait(args.interval):
                return

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop_event.set()
        thread.join()
    return 0


app = create_app()


if __name__ == '__main__':
    main()
