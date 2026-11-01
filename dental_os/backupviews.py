"""The off-site copies page (version 0.50). Managers only. It shows the state; the copies themselves are made by the server's
scheduled job (flask offsite-copy), because a copy should not depend on someone opening a page."""
from flask import Blueprint, current_app, render_template

from .auth import manager_required
from .db import get_db
from .services import offsite as OS

bp = Blueprint("backups", __name__)


@bp.get("/settings/copies")
@manager_required
def home():
    db = get_db()
    cfg = current_app.config
    return render_template("backups.html", st=OS.status(db), target=bool(cfg.get("OFFSITE_TARGET")), locked=bool(cfg.get("OFFSITE_PASSPHRASE")),
                           stale=OS.stale(db), drill=OS.drill_status(db), offline=OS.offline_since(db), read_only=cfg["READ_ONLY"])
