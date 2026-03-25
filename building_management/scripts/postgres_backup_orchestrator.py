#!/usr/bin/env python3
"""PostgreSQL backup orchestration utility.

Best-practice goals implemented:
- Use PostgreSQL native tools through subprocess (`pg_dump`, `pg_basebackup`, `pg_restore`).
- Add checks, upload, retention cleanup, and deploy gating logic in Python.
- Avoid ORM-based dump/restore logic.

Typical usage:
  python scripts/postgres_backup_orchestrator.py backup-logical --upload-s3 s3://bucket/db
  python scripts/postgres_backup_orchestrator.py restore-test --backup-file ./backups/app_20260325_103000.dump
  python scripts/postgres_backup_orchestrator.py deploy-gate --verify-restore -- python manage.py migrate
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


class CommandError(RuntimeError):
    pass


def utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def log(msg: str) -> None:
    print(f"[db-orchestrator] {msg}", flush=True)


def ensure_binary(binary_name: str) -> None:
    if shutil.which(binary_name) is None:
        raise CommandError(f"Required binary not found in PATH: {binary_name}")


def run_cmd(cmd: list[str], env: dict[str, str], cwd: Optional[Path] = None) -> None:
    log("Running: " + " ".join(shlex.quote(x) for x in cmd))
    try:
        subprocess.run(cmd, check=True, env=env, cwd=str(cwd) if cwd else None)
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"Command failed with exit code {exc.returncode}: {' '.join(cmd)}") from exc


def pg_env(password: Optional[str] = None) -> dict[str, str]:
    env = os.environ.copy()
    pg_password = password if password is not None else os.getenv("PGPASSWORD")
    if pg_password:
        env["PGPASSWORD"] = pg_password
    return env


def parse_retention(retention_days: int) -> dt.timedelta:
    if retention_days < 1:
        raise CommandError("Retention days must be >= 1")
    return dt.timedelta(days=retention_days)


def cleanup_old_files(directory: Path, retention_days: int, patterns: Iterable[str]) -> list[Path]:
    threshold = dt.datetime.now(dt.timezone.utc) - parse_retention(retention_days)
    removed: list[Path] = []
    for pattern in patterns:
        for path in directory.glob(pattern):
            if not path.is_file():
                continue
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
            if mtime < threshold:
                path.unlink()
                removed.append(path)
    return removed


def upload_to_s3(local_file: Path, s3_uri: str, env: dict[str, str]) -> None:
    ensure_binary("aws")
    if not s3_uri.startswith("s3://"):
        raise CommandError("--upload-s3 must start with s3://")
    if s3_uri.endswith("/"):
        destination = s3_uri + local_file.name
    else:
        destination = s3_uri
    run_cmd(["aws", "s3", "cp", str(local_file), destination], env=env)


def backup_logical(args: argparse.Namespace) -> Path:
    ensure_binary("pg_dump")
    backup_dir = Path(args.backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    filename = args.file_name or f"{args.db_name}_{utc_timestamp()}.dump"
    output = backup_dir / filename

    env = pg_env(args.db_password)
    cmd = [
        "pg_dump",
        "-h",
        args.db_host,
        "-p",
        str(args.db_port),
        "-U",
        args.db_user,
        "-d",
        args.db_name,
        "-Fc",
        "-f",
        str(output),
    ]
    run_cmd(cmd, env=env)

    if not output.exists() or output.stat().st_size == 0:
        raise CommandError(f"Backup output was not created or empty: {output}")

    log(f"Logical backup created: {output}")

    if args.upload_s3:
        upload_to_s3(output, args.upload_s3, env)
        log(f"Uploaded logical backup to {args.upload_s3}")

    removed = cleanup_old_files(backup_dir, args.retention_days, ["*.dump"])
    if removed:
        log(f"Retention cleanup removed {len(removed)} old backup file(s)")

    return output


def backup_base(args: argparse.Namespace) -> Path:
    ensure_binary("pg_basebackup")
    base_dir = Path(args.basebackup_dir).resolve() / f"basebackup_{utc_timestamp()}"
    base_dir.mkdir(parents=True, exist_ok=True)

    env = pg_env(args.db_password)
    cmd = [
        "pg_basebackup",
        "-h",
        args.db_host,
        "-p",
        str(args.db_port),
        "-U",
        args.db_user,
        "-D",
        str(base_dir),
        "-Fp",  # plain format directory
        "-X",
        "stream",  # include WAL stream
        "-c",
        "fast",  # checkpoint fast for shorter backup window
    ]
    run_cmd(cmd, env=env)

    if not any(base_dir.iterdir()):
        raise CommandError(f"Base backup appears empty: {base_dir}")

    log(f"Base backup created: {base_dir}")

    if args.upload_s3:
        # package folder before upload for atomic transfer
        archive = shutil.make_archive(str(base_dir), "gztar", root_dir=base_dir.parent, base_dir=base_dir.name)
        archive_path = Path(archive)
        upload_to_s3(archive_path, args.upload_s3, env)
        log(f"Uploaded base backup archive to {args.upload_s3}")

    removed = cleanup_old_files(Path(args.basebackup_dir).resolve(), args.retention_days, ["basebackup_*", "*.tar.gz"])
    if removed:
        log(f"Retention cleanup removed {len(removed)} old base backup artifact(s)")

    return base_dir


def restore_test(args: argparse.Namespace) -> None:
    ensure_binary("dropdb")
    ensure_binary("createdb")
    ensure_binary("pg_restore")

    backup_file = Path(args.backup_file).resolve()
    if not backup_file.exists():
        raise CommandError(f"Backup file not found: {backup_file}")

    env = pg_env(args.db_password)

    drop_cmd = [
        "dropdb",
        "-h",
        args.db_host,
        "-p",
        str(args.db_port),
        "-U",
        args.db_user,
        "--if-exists",
        args.restore_db,
    ]
    run_cmd(drop_cmd, env=env)

    create_cmd = [
        "createdb",
        "-h",
        args.db_host,
        "-p",
        str(args.db_port),
        "-U",
        args.db_user,
        args.restore_db,
    ]
    run_cmd(create_cmd, env=env)

    restore_cmd = [
        "pg_restore",
        "-h",
        args.db_host,
        "-p",
        str(args.db_port),
        "-U",
        args.db_user,
        "-d",
        args.restore_db,
        "--clean",
        "--if-exists",
        str(backup_file),
    ]
    run_cmd(restore_cmd, env=env)

    log(f"Restore verification succeeded into database: {args.restore_db}")



def deploy_gate(args: argparse.Namespace) -> None:
    if not args.migrate_cmd:
        raise CommandError("deploy-gate requires a migration command after '--'")

    log("Starting pre-deploy backup gate")
    backup_path = backup_logical(args)

    if args.verify_restore:
        restore_args = argparse.Namespace(
            backup_file=str(backup_path),
            db_host=args.db_host,
            db_port=args.db_port,
            db_user=args.db_user,
            db_password=args.db_password,
            restore_db=args.restore_db,
        )
        restore_test(restore_args)

    env = pg_env(args.db_password)
    run_cmd(args.migrate_cmd, env=env)
    log("Migration command finished successfully")



def add_common_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "postgres"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD"))



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PostgreSQL backup orchestration utility")
    sub = parser.add_subparsers(dest="command", required=True)

    p_logical = sub.add_parser("backup-logical", help="Create pg_dump custom-format backup")
    add_common_db_args(p_logical)
    p_logical.add_argument("--backup-dir", default=os.getenv("BACKUP_DIR", "./backups"))
    p_logical.add_argument("--file-name", default=None)
    p_logical.add_argument("--retention-days", type=int, default=int(os.getenv("BACKUP_RETENTION_DAYS", "14")))
    p_logical.add_argument("--upload-s3", default=os.getenv("BACKUP_S3_URI"))
    p_logical.set_defaults(func=backup_logical)

    p_base = sub.add_parser("backup-base", help="Create pg_basebackup base backup directory")
    add_common_db_args(p_base)
    p_base.add_argument("--basebackup-dir", default=os.getenv("BASEBACKUP_DIR", "./basebackups"))
    p_base.add_argument("--retention-days", type=int, default=int(os.getenv("BASEBACKUP_RETENTION_DAYS", "7")))
    p_base.add_argument("--upload-s3", default=os.getenv("BASEBACKUP_S3_URI"))
    p_base.set_defaults(func=backup_base)

    p_restore = sub.add_parser("restore-test", help="Restore a .dump file into a test DB for verification")
    p_restore.add_argument("--backup-file", required=True)
    p_restore.add_argument("--restore-db", default=os.getenv("RESTORE_TEST_DB", "restore_verification"))
    p_restore.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    p_restore.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "5432")))
    p_restore.add_argument("--db-user", default=os.getenv("DB_USER", "postgres"))
    p_restore.add_argument("--db-password", default=os.getenv("DB_PASSWORD"))
    p_restore.set_defaults(func=restore_test)

    p_gate = sub.add_parser("deploy-gate", help="Run backup gate then migration command")
    add_common_db_args(p_gate)
    p_gate.add_argument("--backup-dir", default=os.getenv("BACKUP_DIR", "./backups"))
    p_gate.add_argument("--file-name", default=None)
    p_gate.add_argument("--retention-days", type=int, default=int(os.getenv("BACKUP_RETENTION_DAYS", "14")))
    p_gate.add_argument("--upload-s3", default=os.getenv("BACKUP_S3_URI"))
    p_gate.add_argument("--verify-restore", action="store_true")
    p_gate.add_argument("--restore-db", default=os.getenv("RESTORE_TEST_DB", "restore_verification"))
    p_gate.add_argument(
        "migrate_cmd",
        nargs=argparse.REMAINDER,
        help="Migration command appended after '--', e.g. -- python manage.py migrate",
    )
    p_gate.set_defaults(func=deploy_gate)

    return parser



def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Strip a leading '--' from REMAINDER if present.
    if hasattr(args, "migrate_cmd") and args.migrate_cmd and args.migrate_cmd[0] == "--":
        args.migrate_cmd = args.migrate_cmd[1:]

    try:
        result = args.func(args)
        if isinstance(result, Path):
            log(f"Done: {result}")
        return 0
    except CommandError as exc:
        log(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        log("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
