#!/usr/bin/env python3
"""Local console profiles: shared installs, private standard save locations.

Profiles share the Linux uid; this is save separation, not a security boundary.
Only owned save directories are redirected. Game binaries are never copied.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

_locks = threading.local()


def root():
    return Path(os.environ.get("MARWANOS_PROFILES_HOME", str(Path.home() / ".local/share/marwanos/profiles")))


def state():
    path = root() / "users.json"
    if not path.exists():
        return {"schema_version": 1, "active": "owner", "users": [{"id": "owner", "name": "Player"}]}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("users"), list):
        raise ValueError("User profiles could not be read. Restore users.json before playing.")
    ids = []
    for row in data["users"]:
        key = row.get("id") if isinstance(row, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"owner|user-[a-f0-9]{32}", key) or key in ids:
            raise ValueError("Invalid user profiles. Restore users.json before playing.")
        ids.append(key)
    if "owner" not in ids or data.get("active") not in ids:
        raise ValueError("Invalid user profiles. Restore users.json before playing.")
    return data


def active_id():
    data = state()
    key = os.environ.get("MARWANOS_PROFILE_ID") or data.get("active", "owner")
    return validate(key, data)


def validate(key, data=None):
    if not isinstance(key, str) or not re.fullmatch(r"owner|user-[a-f0-9]{32}", key):
        raise ValueError("Invalid user profile.")
    if key not in [row.get("id") for row in (data or state())["users"] if isinstance(row, dict)]:
        raise ValueError("Choose a user before playing.")
    return key


def data_home(key):
    validate(key)
    path = root() / key
    if path.is_symlink() or root().is_symlink():
        raise ValueError("The user data directory must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextlib.contextmanager
def lock():
    import fcntl
    if getattr(_locks, "held", False):
        yield
        return
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root() / "saves.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        _locks.held = True
        try:
            yield
        finally:
            _locks.held = False


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bind_save(source, key, namespace, directory=True, legacy_key="owner"):
    """Migrate legacy saves to owner, then atomically replace one save link.

    A crash between migration and linking leaves the original data in owner;
    repeating the operation recovers it. Unknown links are never overwritten.
    """
    source = Path(source)
    digest = hashlib.sha256(str(source.absolute()).encode()).hexdigest()
    owner = data_home(legacy_key) / "saves" / namespace / digest
    destination = data_home(key) / "saves" / namespace / digest
    owner.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    binding = root() / "bindings" / (digest + ".json")
    if source.is_symlink():
        target = source.resolve()
        allowed = [data_home(row["id"]) / "saves" / namespace / digest
                   for row in state()["users"]]
        if target not in allowed:
            raise ValueError("A save location points outside the profile store: " + str(source))
    elif source.exists():
        # Wine commits user.reg by replacing the file, which can replace our
        # symlink. The durable binding records who owns that new registry.
        if not directory and binding.exists():
            previous = validate(json.loads(binding.read_text())["profile"])
            owner = data_home(previous) / "saves" / namespace / digest
            source.replace(owner)
        elif owner.exists():
            raise ValueError("Both legacy and profile saves exist. Resolve the conflict before playing: " + str(source))
        elif source.is_dir() != directory:
            raise ValueError("Invalid save location: " + str(source))
        else:
            source.rename(owner)
    if not destination.exists():
        if directory:
            destination.mkdir(mode=0o700)
        else:
            destination.write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    if directory and namespace == "wine-users":
        # Regular folders prevent Wine's usual Documents -> shared HOME links.
        for username in ("steamuser", os.environ.get("USER", "player")):
            for folder in ("Documents", "Desktop", "Downloads", "Music", "Pictures", "Videos", "Saved Games", "AppData/Local", "AppData/LocalLow", "AppData/Roaming"):
                (destination / username / folder).mkdir(parents=True, exist_ok=True)
        (destination / "Public/Documents").mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    binding.parent.mkdir(parents=True, exist_ok=True)
    write_json(binding, {"profile": key})
    temporary = source.with_name(source.name + ".profile-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(destination, target_is_directory=directory)
        temporary.replace(source)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_windows(prefix, key=None):
    key = validate(key or active_id())
    prefix = Path(prefix)
    with lock():
        bind_save(prefix / "drive_c/users", key, "wine-users")
        bind_save(prefix / "user.reg", key, "wine-registry", directory=False)


def prepare_installed_windows(key):
    base = Path(os.environ.get("MARWANOS_WINDOWS_HOME", str(Path.home() / ".local/share/marwanos/windows")))
    for path in (base / "apps").glob("*.json"):
        entry = json.loads(path.read_text())
        if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str):
            raise ValueError("The application's stored save location is invalid.")
        prefix = Path(entry["prefix"])
        # The installer owns this path; reject custom or redirected roots.
        if (prefix.parent != base / "prefixes" or prefix.is_symlink() or
                prefix.parent.is_symlink() or not re.fullmatch(r"[a-z0-9-]+", prefix.name)):
            raise ValueError("The application's stored save location is invalid.")
        prepare_windows(prefix, key)


def activate(key):
    validate(key)
    # The shell rejects live/minimized games and installers before this call.
    # Steam's account, client and saves belong to the shared console session.
    with lock():
        registry = state()
        previous = registry.get("active", "owner")
        try:
            prepare_installed_windows(key)
            registry["active"] = key
            write_json(root() / "users.json", registry)
        except Exception:
            prepare_installed_windows(previous)
            raise


def game_environment(key):
    env = dict(os.environ, MARWANOS_PROFILE_ID=key, MARWANOS_PROFILES_HOME=str(root()))
    if key != "owner":
        home = data_home(key) / "home"
        home.mkdir(exist_ok=True)
        env.update(HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["activate", "run", "run-windows"])
    parser.add_argument("profile")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        key = validate(args.profile)
        if args.command == "activate":
            # Request result is separate from the durable user registry.
            result = Path(args.arguments[0])
            try:
                activate(key)
                value = {"ok": True}
            except (OSError, ValueError) as error:
                value = {"ok": False, "error": str(error)}
            result.write_text(json.dumps(value))
            return 0 if value["ok"] else 1
        argv = args.arguments
        if argv[:1] == ["--"]:
            argv = argv[1:]
        if not argv:
            raise ValueError("No game command supplied.")
        if args.command == "run-windows":
            slug, *argv = argv
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug):
                raise ValueError("Invalid game ID.")
            env = game_environment(key)
            if key != "owner":
                env["WINEPREFIX"] = str(data_home(key) / "standalone-prefixes" / slug)
            os.execvpe(argv[0], argv, env)
        os.execvpe(argv[0], argv, game_environment(key))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
