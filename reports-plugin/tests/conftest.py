# tests/conftest.py
# This repo has no test suite yet, and ots_citrap_report/app.py has a hard,
# unconditional `from opentakserver.plugins.Plugin import Plugin` (unlike
# ots-federation's plugin.py, which degrades gracefully outside the OTS
# venv). Pulling in the real (heavy) opentakserver package just to run
# tests isn't reasonable, so this stubs the minimal surface app.py actually
# touches: Plugin (base class with name/distro/routes/metadata attributes)
# and extensions.db/logger (a real Flask-SQLAlchemy instance and a plain
# logger). Installed into sys.modules before any test imports
# ots_citrap_report.app, since Python caches that import on first use.

import logging
import sys
import types

import pytest


def _install_opentakserver_stub():
    if "opentakserver" in sys.modules:
        return

    from flask_sqlalchemy import SQLAlchemy

    opentakserver = types.ModuleType("opentakserver")
    plugins_pkg = types.ModuleType("opentakserver.plugins")
    plugin_mod = types.ModuleType("opentakserver.plugins.Plugin")
    extensions_mod = types.ModuleType("opentakserver.extensions")

    class Plugin:
        def __init__(self):
            self.name = ""
            self.distro = ""
            self.routes = []
            self.metadata = {}
            self._app = None

    plugin_mod.Plugin = Plugin
    extensions_mod.db = SQLAlchemy()
    extensions_mod.logger = logging.getLogger("OpenTAKServer")

    opentakserver.plugins = plugins_pkg
    opentakserver.extensions = extensions_mod
    plugins_pkg.Plugin = plugin_mod

    sys.modules["opentakserver"] = opentakserver
    sys.modules["opentakserver.plugins"] = plugins_pkg
    sys.modules["opentakserver.plugins.Plugin"] = plugin_mod
    sys.modules["opentakserver.extensions"] = extensions_mod


_install_opentakserver_stub()

from opentakserver.extensions import db  # noqa: E402  (must follow the stub install above)


def _setup_models_once():
    """Define the Flask-Security user/role models and datastore exactly
    once per process. Flask-SQLAlchemy's db.Model shares one declarative
    metadata across the whole process (our stub's `db` is a module-level
    singleton, matching the real opentakserver.extensions.db it stands in
    for) -- redefining these classes again in a second test would try to
    redefine the same tables on that shared metadata and raise
    InvalidRequestError. The datastore/model classes don't need to be
    per-app; only the Flask app + Security(app, ...) binding is per-test."""
    if hasattr(db, "_test_user_model"):
        return

    from flask_security.models import fsqla_v3 as fsqla

    fsqla.FsModels.set_db_info(db)

    class Role(db.Model, fsqla.FsRoleMixin):
        pass

    class User(db.Model, fsqla.FsUserMixin):
        pass

    db._test_role_model = Role
    db._test_user_model = User


_setup_models_once()


@pytest.fixture
def app_and_db():
    """A real Flask app + real Flask-Security-Too + real in-memory SQLite,
    with a logged-in test user available. Exercises the actual auth_required
    decorator our new routes use, not a bypassed/mocked version of it."""
    from flask import Flask
    from flask_security import Security, SQLAlchemyUserDatastore, hash_password

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SECRET_KEY="test",
        SECURITY_PASSWORD_SALT="test-salt",
        WTF_CSRF_ENABLED=False,
    )
    db.init_app(app)

    Role = db._test_role_model
    User = db._test_user_model
    user_datastore = SQLAlchemyUserDatastore(db, User, Role)
    Security(app, user_datastore)

    with app.app_context():
        db.create_all()
        user_datastore.create_user(email="admin@example.com", password=hash_password("testpassword123"))
        db.session.commit()

    yield app, db

    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def activated_app(app_and_db):
    """app_and_db, with the plugin activated and both blueprints registered
    -- done here, before any client fixture below makes its first request,
    since Flask forbids register_blueprint() after that point (exactly the
    late-enable failure mode _register_official_routes's own error message
    warns about)."""
    from ots_citrap_report.app import OtsCitrapReport

    app, _db = app_and_db
    plugin = OtsCitrapReport()
    plugin.activate(app, True)
    app.register_blueprint(plugin.blueprint)
    return app, plugin


@pytest.fixture
def client(activated_app):
    app, _plugin = activated_app
    return app.test_client()


@pytest.fixture
def logged_in_client(client):
    client.post("/login", data={"email": "admin@example.com", "password": "testpassword123"}, follow_redirects=True)
    return client
