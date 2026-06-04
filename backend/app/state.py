import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest import ReleaseManifest

SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_state (
  id           INTEGER PRIMARY KEY CHECK(id = 1),
  device_id    TEXT NOT NULL,
  device_token TEXT NOT NULL,
  tunnel_ip    TEXT NOT NULL,
  wg_iface     TEXT NOT NULL,
  enrolled_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  release     TEXT NOT NULL,
  status      TEXT NOT NULL CHECK(status IN ('running','success','failed')),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  log         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS releases (
  release           TEXT PRIMARY KEY,
  community_ref     TEXT NOT NULL,
  built_at          TEXT,
  active            INTEGER DEFAULT 0,
  firstboot_secret  TEXT
);

CREATE TABLE IF NOT EXISTS templates (
  release        TEXT NOT NULL REFERENCES releases(release),
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,
  template_vmid  INTEGER NOT NULL,
  app_version    TEXT,
  builder_ref    TEXT,
  built_at       TEXT,
  PRIMARY KEY (release, name)
);

CREATE TABLE IF NOT EXISTS projects (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  site_name   TEXT NOT NULL,
  release     TEXT NOT NULL REFERENCES releases(release),
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES projects(id),
  type         TEXT NOT NULL,
  vmid         INTEGER NOT NULL,
  hostname     TEXT NOT NULL UNIQUE,
  status       TEXT NOT NULL CHECK(status IN ('pending','provisioning','active','error')),
  wire_to      TEXT,
  created_at   TEXT NOT NULL,
  activated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self._init()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migration for DBs created before firstboot_secret column was added
            try:
                conn.execute("ALTER TABLE releases ADD COLUMN firstboot_secret TEXT")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- releases ---

    def upsert_release(self, release: str, community_ref: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO releases (release, community_ref, firstboot_secret)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(release) DO UPDATE SET"
                "   community_ref = excluded.community_ref,"
                # Keep existing secret if already set; only assign on first insert
                "   firstboot_secret = COALESCE(releases.firstboot_secret, excluded.firstboot_secret)",
                (release, community_ref, secrets.token_hex(16)),
            )

    def set_active_release(self, release: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE releases SET active = 0")
            conn.execute("UPDATE releases SET active = 1 WHERE release = ?", (release,))

    def get_release(self, release: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM releases WHERE release = ?", (release,)
            ).fetchone()

    def list_releases(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM releases ORDER BY release DESC"
            ).fetchall()

    def active_release(self) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM releases WHERE active = 1"
            ).fetchone()

    # --- templates ---

    def record_template(
        self,
        release: str,
        name: str,
        kind: str,
        template_vmid: int,
        app_version: str | None,
        builder_ref: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO templates"
                " (release, name, kind, template_vmid, app_version, builder_ref, built_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(release, name) DO UPDATE SET"
                "   template_vmid = excluded.template_vmid,"
                "   app_version   = excluded.app_version,"
                "   builder_ref   = excluded.builder_ref,"
                "   built_at      = excluded.built_at",
                (release, name, kind, template_vmid, app_version, builder_ref, _now()),
            )

    def get_template(self, release: str, name: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM templates WHERE release = ? AND name = ?",
                (release, name),
            ).fetchone()

    def list_templates(self, release: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM templates WHERE release = ? ORDER BY name",
                (release,),
            ).fetchall()

    # --- projects ---

    def create_project(self, site_name: str, release: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO projects (site_name, release, created_at) VALUES (?, ?, ?)",
                (site_name, release, _now()),
            )
            return cur.lastrowid

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()

    def list_projects(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()

    # --- instances ---

    def create_instance(
        self,
        project_id: int,
        type_: str,
        vmid: int,
        hostname: str,
        wire_to: dict[str, Any] | None = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO instances"
                " (project_id, type, vmid, hostname, status, wire_to, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (project_id, type_, vmid, hostname, json.dumps(wire_to) if wire_to else None, _now()),
            )
            return cur.lastrowid

    def set_instance_status(self, hostname: str, status: str) -> None:
        activated_at = _now() if status == "active" else None
        with self._conn() as conn:
            conn.execute(
                "UPDATE instances SET status = ?, activated_at = COALESCE(?, activated_at)"
                " WHERE hostname = ?",
                (status, activated_at, hostname),
            )

    def get_instance_by_vmid(self, vmid: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT i.*, p.site_name, p.release"
                " FROM instances i JOIN projects p ON p.id = i.project_id"
                " WHERE i.vmid = ?",
                (vmid,),
            ).fetchone()

    def get_instance_by_hostname(self, hostname: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM instances WHERE hostname = ?", (hostname,)
            ).fetchone()

    def list_instances(self, project_id: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM instances WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()

    # --- builds ---

    def create_build(self, release: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO builds (release, status, started_at) VALUES (?, 'running', ?)",
                (release, _now()),
            )
            return cur.lastrowid

    def append_build_log(self, build_id: int, text: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE builds SET log = log || ? WHERE id = ?",
                (text, build_id),
            )

    def finish_build(self, build_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE builds SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), build_id),
            )

    def get_build(self, build_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM builds WHERE id = ?", (build_id,)
            ).fetchone()

    def list_builds(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT id, release, status, started_at, finished_at FROM builds ORDER BY id DESC"
            ).fetchall()

    # --- instances ---

    def list_all_instances(self) -> list[sqlite3.Row]:
        """All instances joined with their project (site_name, release)."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT i.*, p.site_name, p.release"
                " FROM instances i JOIN projects p ON p.id = i.project_id"
                " ORDER BY p.site_name, i.hostname"
            ).fetchall()

    # --- seed from manifest ---

    # --- platform state ---

    def get_platform_state(self) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM platform_state WHERE id = 1").fetchone()

    def set_platform_state(
        self, device_id: str, device_token: str, tunnel_ip: str, wg_iface: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO platform_state (id, device_id, device_token, tunnel_ip, wg_iface, enrolled_at)"
                " VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   device_id = excluded.device_id,"
                "   device_token = excluded.device_token,"
                "   tunnel_ip = excluded.tunnel_ip,"
                "   wg_iface = excluded.wg_iface,"
                "   enrolled_at = excluded.enrolled_at",
                (device_id, device_token, tunnel_ip, wg_iface, _now()),
            )

    # --- seed from manifest ---

    def seed_release(self, manifest: ReleaseManifest) -> None:
        self.upsert_release(manifest.release, manifest.community_scripts_ref)
