"""What this checkout declares for ClickHouse users and their grants, shared by
tests/clickhouse-users-docker.py (which proves the fixture against the rendered chart and a real
ClickHouse on every change) and tests/clickstack-upgrade-kind.py (which compares an upgraded cluster
with it). No third-party imports: the kind job runs without PyYAML.
"""
import json
import pathlib
import re

# Every user's grants, byte for byte: the rendered `grants` list and the database's SHOW GRANTS.
GRANTS_FIXTURE = "tests/fixtures/clickhouse-users-grants.json"


def parse_grant(stmt):
    """'GRANT a, b ON x, c ON y' -> {x: {a, b}, y: {c}}. Privilege lists never contain ' ON '."""
    body = re.sub(r"\s+", " ", stmt).strip()
    if not body.startswith("GRANT "):
        return None
    body = body[len("GRANT "):]
    out = {}
    for m in re.finditer(r"\s*(.+?)\s+ON\s+([\w.*]+)\s*(?:,|$)", body):
        out.setdefault(m.group(2), set()).update(p.strip() for p in m.group(1).split(","))
    return out


def privset(s):
    return {p.strip().replace("ALTER MODIFY TTL", "ALTER TTL") for p in s.split(",")}


def fixture(root):
    return json.loads((pathlib.Path(root) / GRANTS_FIXTURE).read_text())


def declared_users(root):
    """Every user ClickHouse should hold: the chart's users.d entries plus the built-in `default`."""
    return sorted(set(fixture(root)["rendered"]) | {"default"})


def declared_grants(root):
    """user -> SHOW GRANTS lines as the database prints them, for the users the fixture's SHOW GRANTS
    covers (every password user; `default` and the passwordless exec_reader have no line there)."""
    return fixture(root)["show_grants"]


def declared_privileges(root, user):
    """user -> {object: privileges} from the rendered `grants` statement, or None."""
    g = fixture(root)["rendered"].get(user) or []
    out = {}
    for item in g:
        for o, p in (parse_grant(item["query"]) or {}).items():
            out.setdefault(o, set()).update(privset(", ".join(p)))
    return out or None


def lines_to_privileges(lines):
    """SHOW GRANTS lines ('GRANT a, b ON x TO user') -> {object: privileges}."""
    out = {}
    for l in lines:
        m = re.match(r"GRANT (.+?) ON ([\w.*]+) TO \S+$", l.strip())
        if m:
            out.setdefault(m.group(2), set()).update(privset(m.group(1)))
    return out
