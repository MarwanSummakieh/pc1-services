#!/usr/bin/env python3
"""Player-owned automatic metadata. Installation identity and launch commands stay local."""
import argparse
import hashlib
import html
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request

BASE = Path(os.environ.get("MARWANOS_METADATA_HOME", str(Path.home() / ".local/share/marwanos/metadata")))
WINDOWS = Path(os.environ.get("MARWANOS_WINDOWS_HOME", str(Path.home() / ".local/share/marwanos/windows")))
APPS = Path(os.environ.get("MARWANOS_METADATA_APPS", "/run/marwanos/apps.tsv"))
RETRY_SECONDS = 900
LOG = logging.getLogger("pc1.metadata")


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        temp.write(data)
        temp.flush()
        os.fsync(temp.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def normalized(title):
    # Do not discard edition words or numbers: a demo or sequel is a different match.
    value = unicodedata.normalize("NFKD", title).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


class SteamProvider:
    name = "Steam Store"
    cdn = "https://cdn.akamai.steamstatic.com/steam/apps"

    def fetch(self, url, limit=4 * 1024 * 1024):
        request = urllib.request.Request(url, headers={"User-Agent": "PC1-Metadata/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            # Only public Steam endpoints/artwork are requested, including redirects.
            host = urllib.parse.urlparse(response.url).hostname or ""
            if not (host == "store.steampowered.com" or host.endswith(".steamstatic.com")):
                raise ValueError("Unexpected metadata host")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Metadata response too large")
        return data

    def json(self, url):
        return json.loads(self.fetch(url).decode("utf-8"))

    def search(self, title):
        query = urllib.parse.urlencode({"term": title[:200], "l": "english", "cc": "us"})
        url = "https://store.steampowered.com/api/storesearch/?" + query
        values = self.json(url).get("items", [])
        return [{"provider_id": str(item["id"]), "title": str(item["name"]), "search_url": url}
                for item in values if str(item.get("id", "")).isdigit() and item.get("name")][:20]

    def details(self, appid):
        if not str(appid).isdigit():
            raise ValueError("Invalid Steam metadata ID")
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=us"
        item = self.json(url).get(str(appid), {})
        if not item.get("success") or item.get("data", {}).get("type") != "game":
            raise ValueError("Selected entry is not an available game")
        data = item["data"]
        text = html.unescape(re.sub(r"<[^>]+>", "", data.get("short_description", "")))
        return {"provider": self.name, "provider_id": str(appid), "metadata_url": url,
                "title": data["name"], "description": text,
                "release_date": data.get("release_date", {}).get("date", ""),
                "genres": [item["description"] for item in data.get("genres", [])],
                "developers": data.get("developers", []), "publishers": data.get("publishers", []),
                "platforms": [key for key, value in data.get("platforms", {}).items() if value],
                "store_url": f"https://store.steampowered.com/app/{appid}/",
                "art_urls": {"cover": f"{self.cdn}/{appid}/library_600x900.jpg",
                             "background": data.get("background_raw") or f"{self.cdn}/{appid}/library_hero.jpg",
                             "logo": f"{self.cdn}/{appid}/logo.png",
                             "header": data.get("header_image", "")}}


class Manager:
    def __init__(self, base=BASE, provider=None):
        self.base = Path(base)
        self.provider = provider or SteamProvider()
        self.state = read_json(self.base / "state.json", {"schema_version": 1, "games": {}})
        self.state.setdefault("games", {})
        for record in self.state["games"].values():
            if record.get("status") == "loading":
                record.update(status="error", attempted_at=0)

    def publish(self):
        self.state["updated_at"] = time.time()
        atomic_json(self.base / "state.json", self.state)

    def artwork(self, metadata, previous):
        from PIL import Image
        assets, failures = {}, []
        for kind, url in metadata["art_urls"].items():
            if not url:
                continue
            try:
                data = self.provider.fetch(url, 12 * 1024 * 1024)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > 24000000:
                        raise ValueError("Unsupported artwork")
                    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
                    image.verify()
                digest = hashlib.sha256(data).hexdigest()
                path = self.base / "assets" / (digest + suffix)
                atomic_bytes(path, data)
                assets[kind] = {"path": str(path), "url": url, "sha256": digest,
                                "bytes": len(data), "downloaded_at": time.time()}
                LOG.info("downloaded %s %s (%d bytes)", kind, url, len(data))
            except Exception as error:
                failures.append(kind)
                old = previous.get("assets", {}).get(kind, {})
                # A corrected match must never inherit the old game's artwork.
                if old.get("url") == url and Path(old.get("path", "")).is_file():
                    assets[kind] = old
                LOG.warning("artwork %s: %s", kind, error)
        return assets, failures

    def enrich(self, entry, force=False, match=None):
        key = entry["id"]
        games = self.state["games"]
        old = games.get(key, {})
        title = entry.get("title", "")
        if not force and old.get("import_title") == title:
            if old.get("status") in ("needs-match", "unmatched"):
                return
            if old.get("status") == "ready" and all(Path(asset["path"]).is_file()
                                                       for asset in old.get("assets", {}).values()):
                return
            if time.time() - old.get("attempted_at", 0) < RETRY_SECONDS:
                return
        if (match and str(match) != old.get("provider_id")) or (old.get("import_title") != title and not old.get("manual_match")):
            old = {field: old[field] for field in ("overrides", "manual_match") if field in old}
        record = dict(old, import_title=title, source=source_name(key), status="loading",
                      attempted_at=time.time(), error="")
        if match:
            record["manual_match"] = str(match)
        games[key] = record
        self.publish()
        try:
            appid = match or old.get("manual_match")
            if key.startswith("steam.") and not appid:
                appid = key.split(".", 1)[1]
            if not appid:
                candidates = self.provider.search(title)
                record["candidates"] = candidates
                exact = [item for item in candidates if normalized(item["title"]) == normalized(title)]
                if len(exact) != 1:
                    record["status"] = "needs-match" if candidates else "unmatched"
                    self.publish()
                    return
                appid = exact[0]["provider_id"]
            metadata = self.provider.details(str(appid))
            assets, failures = self.artwork(metadata, old)
            metadata.pop("art_urls")
            record.update(metadata, assets=assets, status="partial" if failures else "ready",
                          missing_art=failures, fetched_at=time.time(), candidates=[])
            if match:
                record["manual_match"] = str(match)
            record["error"] = "Some artwork could not be downloaded. Refresh to retry." if failures else ""
        except Exception as error:
            record.update(status="error", error="Metadata download failed. Refresh to retry.")
            LOG.warning("metadata %s: %s", key, error)
        self.publish()

    def request(self, request, entries):
        key = str(request.get("game_id", ""))
        entry = next((entry for entry in entries if entry["id"] == key), None)
        if not entry:
            return
        action = request.get("action")
        if action == "refresh":
            self.enrich(entry, force=True)
        elif action == "match":
            appid = str(request.get("provider_id", ""))
            if appid.isdigit() and len(appid) <= 10:
                self.enrich(entry, force=True, match=appid)
        elif action == "search":
            record = self.state["games"].setdefault(key, {})
            try:
                record["candidates"] = self.provider.search(str(request.get("query", "")))
                record["search_error"] = "" if record["candidates"] else "No matches. Try another title."
            except Exception:
                record["search_error"] = "Search failed. Try again."
            self.publish()
        elif action == "title":
            value = str(request.get("title", "")).strip()[:200]
            record = self.state["games"].setdefault(key, {})
            record.setdefault("overrides", {})["title"] = value
            self.publish()


def source_name(key):
    if "." not in key:
        return "Application"
    return {"steam": "Steam", "managed": "Windows", "win": "Windows", "epic": "Epic",
            "gog": "GOG", "rom": "Emulated"}.get(key.split(".", 1)[0], "Application")


def library(apps=APPS, windows=WINDOWS):
    entries = {}
    try:
        for line in apps.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 6 and fields[5] == "installed" and source_name(fields[0]) != "Application":
                entries[fields[0]] = {"id": fields[0], "title": fields[1]}
    except OSError:
        pass
    for path in (windows / "apps").glob("*.json"):
        entry = read_json(path, {})
        if isinstance(entry, dict) and entry.get("id") and entry.get("state") == "installed":
            entries[entry["id"]] = entry
    return list(entries.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    manager = Manager()
    manager.publish()
    while True:
        entries = library()
        requests = manager.base / "requests"
        for path in sorted(requests.glob("*.json"))[:20]:
            request = read_json(path, {})
            path.unlink(missing_ok=True)
            manager.request(request, entries)
        for entry in entries:
            manager.enrich(entry)
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
