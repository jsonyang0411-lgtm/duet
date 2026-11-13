"""WSGI entry for an external server (optional; ``python3 app.py`` needs none).

Run inside this folder, e.g.::

    gunicorn -w 2 -b 127.0.0.1:8080 wsgi:application
    waitress-serve --listen=127.0.0.1:8080 wsgi:application

Configuration comes from the same SAMPLE_SITE_* environment variables. Do
not switch on the server's access log: it would write the creators' link
tokens. The purge job runs on requests (at most hourly).
"""

import importlib.util
import os
import sys

if "sample_site" not in sys.modules:
    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location("sample_site", os.path.join(_here, "__init__.py"),
                                                   submodule_search_locations=[_here])
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["sample_site"] = _pkg
    _spec.loader.exec_module(_pkg)

from sample_site.app import create_app  # noqa: E402
from sample_site.config import Config  # noqa: E402

application = create_app(Config.from_env())
