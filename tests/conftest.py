"""Shared pytest fixtures.

The three apps each have their own package layout under app/. We add the
inner package roots to sys.path so tests can import the modules directly
without installing each app.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

for sub in (
    REPO_ROOT / "gui_app" / "app",
    REPO_ROOT / "scraper_app" / "app",
    REPO_ROOT / "image_proc_app" / "app",
):
    p = str(sub)
    if p not in sys.path:
        sys.path.insert(0, p)

# Set bare-minimum env so modules that read env at import time don't blow up.
os.environ.setdefault("BUCKET", "test-bucket")
os.environ.setdefault("HTML_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("CUSTOMER", "test-customer")
# Workers build a real boto3.cloudwatch client at import time unless told
# otherwise. Tests should never hit AWS implicitly.
os.environ.setdefault("METRICS_DISABLED", "1")
