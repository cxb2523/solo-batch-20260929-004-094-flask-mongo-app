import os

# Keep the app out of CSRF/Talisman HTTPS enforcement for the offline suite.
os.environ.setdefault("FLASK_TESTING", "1")
