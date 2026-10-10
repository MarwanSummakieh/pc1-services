"""Normalize genuine public catalogs separately from any player's earned state."""
import math
import re
from collections import Counter


def validate_groups(response, rows):
    groups = response.get("groups")
    if groups is None:
        return
    if not isinstance(groups, list):
        raise ValueError("Invalid catalog groups")
    if not groups:
        return
    counts = Counter()
    for row in rows:
        identity = row.get("groupid", 0) if isinstance(row, dict) else None
        if isinstance(identity, bool) or not isinstance(identity, int) or identity < 0:
            raise ValueError("Invalid catalog group ID")
        counts[identity] += 1
    declared = set()
    for group in groups:
        identity = group.get("groupid", 0) if isinstance(group, dict) else None
        if isinstance(identity, bool) or not isinstance(identity, int) or identity < 0 or identity in declared:
            raise ValueError("Invalid catalog group ID")
        declared.add(identity)
        if "total_achievements" not in group:
            continue
        total = group["total_achievements"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != counts[identity]:
            raise ValueError("Public achievement catalog is incomplete")
        # Validate this group's total directly, not a sum or completion target.
        # Archived achievements/groups are retained and count toward their own
        # declared total; completion_achievements is separate provider metadata.
    if set(counts) - declared:
        raise ValueError("Undeclared catalog group")


def catalog_progress(value):
    """Schema bounds are metadata, never progress_current/progress_max."""
    if (not isinstance(value, dict) or isinstance(value.get("type"), bool)
            or not isinstance(value.get("type"), int) or value["type"] not in (1, 2)):
        raise ValueError("Invalid catalog progress bounds")
    low, high = value.get("minimum"), value.get("maximum")
    if any(isinstance(bound, bool) or not isinstance(bound, (int, float))
           or not math.isfinite(bound) or bound < 0 for bound in (low, high)):
        raise ValueError("Invalid catalog progress bounds")
    if high < low or (value["type"] == 1 and any(not isinstance(bound, int) for bound in (low, high))):
        raise ValueError("Invalid catalog progress bounds")
    return {"type": value["type"], "minimum": low, "maximum": high}


def public_schema_rows(value, appid):
    if not isinstance(appid, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", appid) or int(appid) > 0xffffffff:
        raise ValueError("Invalid achievement app ID")
    response = value.get("response") if isinstance(value, dict) else None
    rows = response.get("achievements") if isinstance(response, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 5000:
        raise ValueError("Public achievement catalog is incomplete")
    validate_groups(response, rows)
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Public achievement catalog is incomplete")
        identity, title, description = (row.get(key) for key in ("internal_name", "localized_name", "localized_desc"))
        if (not isinstance(identity, str) or not identity.strip() or len(identity) > 512
                or any(ord(char) < 32 for char in identity) or identity in result
                or not isinstance(title, str) or not title.strip() or len(title) > 2000
                or not isinstance(description, str) or len(description) > 8000
                or not isinstance(row.get("hidden"), bool)):
            raise ValueError("Public achievement catalog is incomplete")
        urls = {}
        for field, output in (("icon", "icon_url"), ("icon_gray", "locked_icon_url")):
            filename = row.get(field)
            if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.(?:jpg|jpeg|png|webp)", filename):
                raise ValueError("Invalid public achievement icon")
            urls[output] = f"https://shared.akamai.steamstatic.com/community_assets/images/apps/{appid}/{filename}"
        normalized = {"id": identity, "name": title, "description": description,
                      "hidden": row["hidden"], **urls}
        progress_type = row.get("progress_type")
        if progress_type is not None and (isinstance(progress_type, bool) or not isinstance(progress_type, int) or progress_type not in (0, 1, 2)):
            raise ValueError("Invalid catalog progress type")
        if progress_type in (1, 2):
            suffix = "int" if progress_type == 1 else "float"
            normalized["catalog_progress"] = catalog_progress({"type": progress_type,
                "minimum": row.get("min_progress_" + suffix), "maximum": row.get("max_progress_" + suffix)})
        result[identity] = normalized
    return result
