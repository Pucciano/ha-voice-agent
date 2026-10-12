"""Build and deploy the wall display dashboards from shared templates.

Every wall display is a storage-mode dashboard in Home Assistant. Its
configuration is merged from the shared button-card templates
(config/homeassistant/dashboards/wall_display_templates.yaml) and a room file
with the room settings and the views. See docs/wall_display.md.

    python scripts/wall_display_deploy.py check
    python scripts/wall_display_deploy.py diff
    python scripts/wall_display_deploy.py deploy
    python scripts/wall_display_deploy.py restore <backup.json>
"""

import argparse
import asyncio
import datetime
import difflib
import json
import os
import pathlib
import re
import sys
from typing import Any, Callable, Iterator, Protocol

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO / "config" / "homeassistant" / "dashboards"
SHARED_FILE = DASHBOARD_DIR / "wall_display_templates.yaml"
# Dashboard url_path in Home Assistant -> room file.
DASHBOARDS = {
    "dashboard-living-room-wall-display": "living_room_wall_display.yaml",
}
TEMPLATES = "button_card_templates"
# Keys of a room file, in the order of the merged configuration.
ROOM_KEYS = ("kiosk_mode", "title", TEMPLATES, "views")
DEFAULT_BACKUP_DIR = pathlib.Path("~/.config/aivi/backups")
BOOL_TAG = "tag:yaml.org,2002:bool"
MERGE_TAG = "tag:yaml.org,2002:merge"

Config = dict[str, Any]


class ConfigError(Exception):
    """A dashboard file is invalid; nothing is deployed."""


class DeployError(Exception):
    """Home Assistant refused a request, or a check after saving failed."""


class HomeAssistantError(DeployError):
    """An error result of a websocket command."""

    def __init__(self, command: str, code: str, message: str) -> None:
        super().__init__(f"{command}: {code}: {message}")
        self.code = code


# ------------------------------------------------------------------ YAML --
class Loader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """Safe loader with YAML 1.2 booleans and unique string keys.

    PyYAML follows YAML 1.1, where on, off, yes and no are booleans. The Home
    Assistant frontend reads them as strings, so a key `on` would silently
    become `true`.
    """


Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver(
    BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_mapping(loader: Loader, node: yaml.MappingNode) -> Config:
    keys: set[str] = set()
    for key_node, _ in node.value:
        if key_node.tag == MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=True)
        line = key_node.start_mark.line + 1
        if not isinstance(key, str):
            raise ConfigError(f"line {line}: key {key!r} is not a string")
        if key in keys:
            raise ConfigError(f"line {line}: duplicate key {key!r}")
        keys.add(key)
    return loader.construct_mapping(node, deep=True)


Loader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


class Dumper(yaml.SafeDumper):
    """Writes multi-line strings (the JavaScript) as literal blocks."""


def _represent_str(dumper: Dumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _represent_str)


def parse_yaml(text: str, source: str) -> Any:
    """Parse one dashboard file."""

    try:
        return yaml.load(text, Loader=Loader)
    except (yaml.YAMLError, ConfigError) as err:
        raise ConfigError(f"{source}: {err}") from err


def load_file(path: pathlib.Path) -> Any:
    """Read and parse one dashboard file."""

    try:
        source = str(path.relative_to(REPO))
    except ValueError:
        source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"{source}: {err.strerror}") from err
    return parse_yaml(text, source)


# ------------------------------------------------------------ validation --
def template_refs(node: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """Yield every template name used below node, with its location."""

    if isinstance(node, dict):
        ref = node.get("template")
        names = ref if isinstance(ref, list) else [] if ref is None else [ref]
        for name in names:
            if not isinstance(name, str):
                raise ConfigError(f"{where}: template names must be strings")
            yield name, where.lstrip(".")
        for key, value in node.items():
            yield from template_refs(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from template_refs(value, f"{where}[{index}]")


def templates_of(data: Config, source: str) -> Config:
    """Return the button-card templates of a file, checked for shape."""

    templates = data.get(TEMPLATES)
    if not isinstance(templates, dict):
        raise ConfigError(f"{source}: {TEMPLATES} must be a mapping")
    for name, body in templates.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{source}: template {name} must be a mapping")
    return templates


def check_inheritance(templates: Config, source: str) -> None:
    """Fail on a template that inherits from itself."""

    def visit(name: str, chain: list[str]) -> None:
        if name in chain:
            cycle = " -> ".join([*chain, name])
            raise ConfigError(f"{source}: template cycle {cycle}")
        parents = templates.get(name, {}).get("template") or []
        for parent in parents if isinstance(parents, list) else [parents]:
            visit(parent, [*chain, name])

    for name in templates:
        visit(name, [])


def merge(shared: Any, room: Any, source: str) -> Config:
    """Merge the shared templates and one room file into a dashboard."""

    if not isinstance(shared, dict) or set(shared) != {TEMPLATES}:
        raise ConfigError(f"{SHARED_FILE.name}: only {TEMPLATES} is allowed")
    shared_templates = templates_of(shared, SHARED_FILE.name)
    if not isinstance(room, dict):
        raise ConfigError(f"{source}: expected a mapping")
    unknown = sorted(set(room) - set(ROOM_KEYS))
    if unknown:
        raise ConfigError(f"{source}: unknown keys {', '.join(unknown)}")
    if not isinstance(room.get("views"), list) or not room["views"]:
        raise ConfigError(f"{source}: views must be a non-empty list")
    room_templates = templates_of(room, source) if TEMPLATES in room else {}
    overrides = sorted(shared_templates.keys() & room_templates.keys())
    if overrides:
        raise ConfigError(
            f"{source}: room templates must not replace shared ones: "
            + ", ".join(overrides)
        )

    config: Config = {}
    for key in ROOM_KEYS:
        if key == TEMPLATES:
            config[key] = {**shared_templates, **room_templates}
        elif key in room:
            config[key] = room[key]

    missing = [
        f"{name} ({where})"
        for name, where in template_refs(config)
        if name not in config[TEMPLATES]
    ]
    if missing:
        raise ConfigError(
            f"{source}: missing templates: " + "; ".join(sorted(set(missing)))
        )
    check_inheritance(config[TEMPLATES], source)
    try:
        json.dumps(config, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"{source}: not JSON compatible: {err}") from err
    return config


def build(only: list[str] | None = None) -> dict[str, Config]:
    """Build the configuration of every (or the given) wall display."""

    shared = load_file(SHARED_FILE)
    return {
        url_path: merge(
            shared,
            load_file(DASHBOARD_DIR / DASHBOARDS[url_path]),
            DASHBOARDS[url_path],
        )
        for url_path in only or DASHBOARDS
    }


def canonical(config: Config | None) -> str:
    """Key-order independent form for comparisons."""

    return json.dumps(config, sort_keys=True, ensure_ascii=False)


def diff_text(url_path: str, old: Config | None, new: Config | None) -> str:
    """Unified diff of two configurations, as YAML."""

    def lines(config: Config | None) -> list[str]:
        if config is None:
            return []
        text = yaml.dump(
            config,
            Dumper=Dumper,
            sort_keys=True,
            allow_unicode=True,
            width=1000,
        )
        return text.splitlines(keepends=True)

    return "".join(
        difflib.unified_diff(
            lines(old), lines(new), f"live/{url_path}", f"new/{url_path}"
        )
    )


# --------------------------------------------------------------- backups --
def write_backup(
    backup_dir: pathlib.Path,
    url_path: str,
    config: Config,
    now: datetime.datetime,
) -> pathlib.Path:
    """Store the live configuration of a dashboard; never overwrites."""

    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "url_path": url_path,
        "saved_at": now.isoformat(),
        "config": config,
    }
    for attempt in range(100):
        suffix = f"-{attempt}" if attempt else ""
        path = backup_dir / f"wall_display_{url_path}_{stamp}{suffix}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path
    raise DeployError(f"{backup_dir}: no free backup file name for {stamp}")


def read_backup(path: pathlib.Path) -> tuple[str, Config]:
    """Return url_path and configuration of a backup file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ConfigError(f"{path}: {err}") from err
    if not isinstance(payload, dict):
        raise ConfigError(f"{path}: not a wall display backup")
    url_path, config = payload.get("url_path"), payload.get("config")
    if not isinstance(url_path, str) or not isinstance(config, dict):
        raise ConfigError(f"{path}: not a wall display backup")
    return url_path, config


# --------------------------------------------------------- Home Assistant --
class Dashboards(Protocol):
    """Storage dashboards of Home Assistant."""

    async def url_paths(self) -> set[str]:
        """Return the url_path of every storage dashboard."""

    async def get(self, url_path: str) -> Config | None:
        """Return the stored configuration, None for an empty dashboard."""

    async def save(self, url_path: str, config: Config) -> None:
        """Replace the stored configuration."""


class HomeAssistant:
    """Dashboards over the Home Assistant websocket API."""

    def __init__(self, url: str, token: str) -> None:
        self._url = re.sub(r"^http", "ws", url.rstrip("/")) + "/api/websocket"
        self._token = token
        self._ws: Any = None
        self._last_id = 0

    async def __aenter__(self) -> "HomeAssistant":
        # Imported here, so the offline commands and the tests need only
        # PyYAML.
        import websockets  # pylint: disable=import-outside-toplevel

        try:
            self._ws = await websockets.connect(self._url, max_size=None)
            await self._ws.recv()
            await self._ws.send(
                json.dumps({"type": "auth", "access_token": self._token})
            )
            reply = json.loads(await self._ws.recv())
        except (OSError, websockets.WebSocketException) as err:
            raise DeployError(
                f"cannot connect to Home Assistant: {err}"
            ) from err
        if reply.get("type") != "auth_ok":
            raise DeployError("Home Assistant rejected the access token")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._ws.close()

    async def _call(self, message: Config) -> Any:
        self._last_id += 1
        await self._ws.send(json.dumps({"id": self._last_id, **message}))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") == self._last_id:
                break
        if not reply.get("success"):
            error = reply.get("error") or {}
            raise HomeAssistantError(
                message["type"],
                str(error.get("code")),
                str(error.get("message")),
            )
        return reply.get("result")

    async def url_paths(self) -> set[str]:
        result = await self._call({"type": "lovelace/dashboards/list"})
        return {
            dashboard["url_path"]
            for dashboard in result
            if dashboard.get("mode") == "storage"
        }

    async def get(self, url_path: str) -> Config | None:
        try:
            return await self._call(
                {"type": "lovelace/config", "url_path": url_path, "force": True}
            )
        except HomeAssistantError as err:
            if err.code == "config_not_found":
                return None
            raise

    async def save(self, url_path: str, config: Config) -> None:
        await self._call(
            {
                "type": "lovelace/config/save",
                "url_path": url_path,
                "config": config,
            }
        )


# -------------------------------------------------------------- commands --
Confirm = Callable[[str], bool]


async def _live(client: Dashboards, targets: list[str]) -> dict[str, Any]:
    """Live configuration of every known wall display that exists."""

    available = await client.url_paths()
    missing = sorted(set(targets) - available)
    if missing:
        raise DeployError(
            "no storage dashboard "
            + ", ".join(missing)
            + " in Home Assistant; create it under Settings -> Dashboards"
        )
    known = [*DASHBOARDS, *[t for t in targets if t not in DASHBOARDS]]
    return {p: await client.get(p) for p in known if p in available}


async def _save(
    client: Dashboards,
    live: dict[str, Any],
    changes: dict[str, Config],
    backup_dir: pathlib.Path,
) -> None:
    """Back up every wall display, then save and verify the changes."""

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    backups = {}
    for url_path, config in live.items():
        if config is None:
            continue
        backups[url_path] = write_backup(backup_dir, url_path, config, now)
        print(f"backup: {backups[url_path]}")
    for url_path, config in changes.items():
        await client.save(url_path, config)
        if canonical(await client.get(url_path)) != canonical(config):
            hint = backups.get(url_path)
            raise DeployError(
                f"{url_path}: stored configuration differs after saving"
                + (f"; restore {hint}" if hint else "")
            )
        print(f"{url_path}: saved")


async def show_diff(client: Dashboards, configs: dict[str, Config]) -> int:
    """Print the difference between Home Assistant and the repository."""

    live = await _live(client, list(configs))
    changed = 0
    for url_path, config in configs.items():
        text = diff_text(url_path, live[url_path], config)
        if text:
            print(text, end="")
            changed += 1
        else:
            print(f"{url_path}: unchanged")
    return changed


async def deploy(
    client: Dashboards,
    configs: dict[str, Config],
    backup_dir: pathlib.Path,
    confirm: Confirm,
) -> list[str]:
    """Save every dashboard that differs; returns their url_paths."""

    live = await _live(client, list(configs))
    changes = {
        url_path: config
        for url_path, config in configs.items()
        if canonical(live[url_path]) != canonical(config)
    }
    for url_path in configs:
        if url_path not in changes:
            print(f"{url_path}: unchanged")
    if not changes:
        return []
    for url_path, config in changes.items():
        print(diff_text(url_path, live[url_path], config), end="")
    if not confirm(f"Save {', '.join(changes)}?"):
        raise DeployError("cancelled; nothing was saved")
    await _save(client, live, changes, backup_dir)
    return list(changes)


async def restore(
    client: Dashboards,
    backup: pathlib.Path,
    backup_dir: pathlib.Path,
    confirm: Confirm,
) -> bool:
    """Save a backup again; the current state is backed up first."""

    url_path, config = read_backup(backup)
    live = await _live(client, [url_path])
    if canonical(live[url_path]) == canonical(config):
        print(f"{url_path}: already matches {backup.name}")
        return False
    print(diff_text(url_path, live[url_path], config), end="")
    if not confirm(f"Restore {url_path} from {backup.name}?"):
        raise DeployError("cancelled; nothing was saved")
    await _save(client, live, {url_path: config}, backup_dir)
    return True


# ------------------------------------------------------------------ main --
def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Read KEY=VALUE lines, as in a shell or compose env file."""

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials(env_file: pathlib.Path | None) -> tuple[str, str]:
    """Home Assistant URL and token; the environment wins over the file."""

    try:
        values = read_env_file(env_file) if env_file else {}
    except OSError as err:
        raise ConfigError(f"{env_file}: {err.strerror}") from err
    url = os.environ.get("HA_URL") or values.get("HA_URL")
    token = os.environ.get("HA_TOKEN") or values.get("HA_TOKEN")
    if not url or not token:
        raise ConfigError("set HA_URL and HA_TOKEN or pass --env-file")
    return url, token


def ask(question: str) -> bool:
    """Ask on the terminal; without one, --yes is required."""

    if not sys.stdin.isatty():
        raise DeployError("no terminal to confirm; pass --yes")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command line arguments."""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--only",
        action="append",
        choices=sorted(DASHBOARDS),
        help="limit to this dashboard (repeatable)",
    )
    online = argparse.ArgumentParser(add_help=False)
    online.add_argument(
        "--env-file",
        type=pathlib.Path,
        help="file with HA_URL and HA_TOKEN",
    )
    online.add_argument(
        "--backup-dir",
        type=pathlib.Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"where backups go (default: {DEFAULT_BACKUP_DIR})",
    )
    online.add_argument(
        "--yes", action="store_true", help="do not ask before saving"
    )
    parser = argparse.ArgumentParser(
        description="Build the wall display dashboards from the shared "
        "templates and deploy them to Home Assistant."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", parents=[common], help="validate offline")
    commands.add_parser(
        "diff", parents=[common, online], help="compare with Home Assistant"
    )
    commands.add_parser(
        "deploy", parents=[common, online], help="save changed dashboards"
    )
    restore_parser = commands.add_parser(
        "restore", parents=[online], help="save a backup again"
    )
    restore_parser.add_argument("backup", type=pathlib.Path)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    """Run one command; returns the exit code."""

    configs = {} if args.command == "restore" else build(args.only)
    if args.command == "check":
        for url_path, config in configs.items():
            print(
                f"{url_path}: ok, {len(config[TEMPLATES])} templates, "
                f"{len(config['views'])} views"
            )
        return 0
    url, token = credentials(args.env_file)
    backup_dir = args.backup_dir.expanduser()
    confirm: Confirm = (lambda _question: True) if args.yes else ask
    async with HomeAssistant(url, token) as client:
        if args.command == "diff":
            await show_diff(client, configs)
        elif args.command == "deploy":
            await deploy(client, configs, backup_dir, confirm)
        else:
            await restore(client, args.backup, backup_dir, confirm)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""

    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except DeployError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
