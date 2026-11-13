"""Sample picking site: a creator picks sample hats and leaves shipping details.

Standard library only (Python 3.9+). The folder is self-contained and can be
copied to a server on its own; see README.md and DEPLOY.md.

In-process use (tests, other tools)::

    from sample_site.app import create_app
    from sample_site.config import Config
    app = create_app(Config(admin_token=secrets.token_urlsafe(32), db_path="/tmp/site.db"))
"""

__version__ = "1.0.0"
