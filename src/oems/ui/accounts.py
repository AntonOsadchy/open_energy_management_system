"""User logins and per-user saved projects ("cases").

Accounts live in ``<workspace>/users.json`` (owner-readable only): a salted PBKDF2 hash per user and the open
sessions (random tokens, sent to the browser as an HttpOnly cookie). Each user's projects are stored under
``<workspace>/users/<user>/projects/<name>.json``; nobody else can list or open them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import time
from pathlib import Path

_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,39}$")
_ITER = 200_000
SESSION_DAYS = 30


class AuthError(Exception):
    pass


def safe_name(name: str) -> str:
    """The file stem a project name is stored under."""
    return re.sub(r"[^\w\-]", "_", name).strip("_") or "project"


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER).hex()


class Accounts:
    def __init__(self, workspace: str | Path):
        self.ws = Path(workspace)
        self.path = self.ws / "users.json"

    def _load(self) -> dict:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        return {"users": data.get("users", {}), "sessions": data.get("sessions", {})}

    def _store(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=1))
        self.path.chmod(0o600)

    def _session(self, data: dict, user: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        data["sessions"] = {t: s for t, s in data["sessions"].items() if s["expires"] > now}
        data["sessions"][token] = {"user": user, "expires": now + SESSION_DAYS * 86400}
        self._store(data)
        return token

    def register(self, user: str, password: str) -> str:
        """Create an account and return a session token. The first account adopts projects saved before logins."""
        user = user.strip()
        if not _USER.match(user):
            raise AuthError("user name: 2-40 letters, digits, '.', '_' or '-', starting with a letter or digit")
        if len(password) < 6:
            raise AuthError("password: at least 6 characters")
        data = self._load()
        if user.lower() in (u.lower() for u in data["users"]):
            raise AuthError(f"user '{user}' already exists")
        first = not data["users"]
        salt = secrets.token_hex(16)
        data["users"][user] = {"salt": salt, "hash": _hash(password, salt), "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        projects = self.projects_dir(user)
        projects.mkdir(parents=True, exist_ok=True)
        legacy = self.ws / "projects"
        if first and legacy.is_dir():
            for p in legacy.glob("*.json"):
                shutil.move(str(p), projects / p.name)
        return self._session(data, user)

    def login(self, user: str, password: str) -> str:
        data = self._load()
        rec = data["users"].get(user.strip())
        if rec is None or not hmac.compare_digest(rec["hash"], _hash(password, rec["salt"])):
            raise AuthError("wrong user name or password")
        return self._session(data, user.strip())

    def logout(self, token: str | None) -> None:
        data = self._load()
        if token and data["sessions"].pop(token, None):
            self._store(data)

    def user_of(self, token: str | None) -> str | None:
        s = self._load()["sessions"].get(token or "")
        return s["user"] if s and s["expires"] > time.time() else None

    # ------------------------------------------------------------- the user's projects
    def projects_dir(self, user: str) -> Path:
        return self.ws / "users" / user / "projects"

    def project_path(self, user: str, name: str) -> Path:
        return self.projects_dir(user) / f"{safe_name(name)}.json"

    def list_projects(self, user: str) -> list[dict]:
        """One record per saved project, most recently saved first."""
        out = []
        for p in self.projects_dir(user).glob("*.json"):
            try:
                proj = json.loads(p.read_text())
            except ValueError:
                continue
            s = proj.get("settings") or {}
            nodes = ((proj.get("drawflow") or {}).get("drawflow") or {}).get("Home", {}).get("data") or {}
            out.append({"id": p.stem, "name": s.get("name") or p.stem, "start": s.get("start"), "days": s.get("days"),
                        "freq": s.get("freq"), "blocks": len(nodes), "saved": p.stat().st_mtime,
                        "parent": proj.get("variant_of")})
        return sorted(out, key=lambda m: m["saved"], reverse=True)

    def relink_variants(self, user: str, old: str, new: str | None) -> None:
        """Point the variants of project ``old`` at ``new`` (its new name, or its own parent when it is deleted)."""
        for p in self.projects_dir(user).glob("*.json"):
            try:
                proj = json.loads(p.read_text())
            except ValueError:
                continue
            if proj.get("variant_of") == old:
                proj["variant_of"] = new
                p.write_text(json.dumps(proj, indent=1))
