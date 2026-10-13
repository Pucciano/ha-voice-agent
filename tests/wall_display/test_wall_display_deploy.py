"""Wall display deploy: YAML rules, validation, merge, backups and saving."""

import asyncio
import copy
import datetime
import pathlib
import stat
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

# pylint: disable=wrong-import-position
import wall_display_deploy as wd  # noqa: E402

SHARED = {
    "button_card_templates": {
        "base": {"show_name": False},
        "tile": {"template": "base", "show_name": True},
    }
}
NOW = datetime.datetime(2026, 10, 12, 8, 0, tzinfo=datetime.timezone.utc)


def room(**extra):
    data = {
        "title": "Test",
        "views": [
            {"cards": [{"type": "custom:button-card", "template": "tile"}]}
        ],
    }
    data.update(extra)
    return data


def merge_error(shared, data):
    with pytest.raises(wd.ConfigError) as info:
        wd.merge(shared, data, "room.yaml")
    return str(info.value)


class FakeDashboards:
    """Storage dashboards in memory."""

    def __init__(self, stored, mangle=False):
        self.stored = copy.deepcopy(stored)
        self.saves = []
        self.mangle = mangle

    async def url_paths(self):
        return set(self.stored)

    async def get(self, url_path):
        return copy.deepcopy(self.stored[url_path])

    async def save(self, url_path, config):
        self.saves.append(url_path)
        self.stored[url_path] = copy.deepcopy(config)
        if self.mangle:
            self.stored[url_path]["title"] = "changed by someone else"


@pytest.fixture(name="two_displays")
def fixture_two_displays(monkeypatch):
    monkeypatch.setattr(
        wd, "DASHBOARDS", {"dash-a": "a.yaml", "dash-b": "b.yaml"}
    )
    return {
        "dash-a": {"title": "A", "views": []},
        "dash-b": {"title": "B", "views": []},
    }


def yes(_question):
    return True


# ------------------------------------------------------------------ YAML --
def test_yaml_1_1_words_stay_strings():
    data = wd.parse_yaml("variables:\n  on: yes\n  off: no\nshow: true\n", "t")

    assert data == {"variables": {"on": "yes", "off": "no"}, "show": True}


def test_duplicate_key_fails():
    with pytest.raises(wd.ConfigError, match="line 2: duplicate key 'a'"):
        wd.parse_yaml("a: 1\na: 2\n", "t")


def test_non_string_key_fails():
    with pytest.raises(wd.ConfigError, match="is not a string"):
        wd.parse_yaml("1: one\n", "t")


# ---------------------------------------------------------------- merge --
def test_merge_keeps_room_settings_and_key_order():
    config = wd.merge(
        SHARED,
        room(
            views=[
                {
                    "cards": [
                        {"type": "custom:button-card", "template": "wd_room"}
                    ]
                }
            ],
            button_card_templates={"wd_room": {"variables": {"area": "x"}}},
            kiosk_mode={"hide_header": True},
        ),
        "room.yaml",
    )

    assert list(config) == [
        "kiosk_mode",
        "title",
        "button_card_templates",
        "views",
    ]
    assert list(config["button_card_templates"]) == ["base", "tile", "wd_room"]
    assert config["kiosk_mode"] == {"hide_header": True}


def test_room_cannot_replace_a_shared_template():
    error = merge_error(SHARED, room(button_card_templates={"tile": {}}))

    assert "must not replace shared ones: tile" in error


def test_missing_template_is_reported_with_its_place():
    nested = {
        "type": "custom:button-card",
        "template": "tile",
        "custom_fields": {"x": {"card": {"template": ["base", "nope"]}}},
    }

    error = merge_error(SHARED, room(views=[{"cards": [nested]}]))

    assert (
        "missing templates: nope (views[0].cards[0].custom_fields.x.card)"
        in error
    )


def test_template_cycle_fails():
    shared = {
        "button_card_templates": {
            "a": {"template": "b"},
            "b": {"template": ["a"]},
        }
    }

    error = merge_error(shared, room(views=[{"cards": [{"template": "a"}]}]))

    assert "template cycle a -> b -> a" in error


def test_shared_file_holds_only_templates():
    error = merge_error({**SHARED, "views": []}, room())

    assert "only button_card_templates is allowed" in error


def test_unknown_room_key_fails():
    assert "unknown keys theme" in merge_error(SHARED, room(theme="x"))


def test_room_needs_views():
    assert "views must be a non-empty list" in merge_error(
        SHARED, room(views=[])
    )


def test_values_must_be_json_compatible():
    dated = wd.parse_yaml("title: 2026-10-12\n", "t")

    assert "not JSON compatible" in merge_error(SHARED, room(**dated))


def test_repository_dashboards_build():
    configs = wd.build()

    assert set(configs) == set(wd.DASHBOARDS)
    for config in configs.values():
        assert config["views"][0]["cards"][0]["template"] == "wd_root"
        room_settings = config["button_card_templates"]["wd_room"]
        assert room_settings["variables"]["home_area"]


# -------------------------------------------------------------- backups --
def test_backups_never_overwrite_and_stay_private(tmp_path):
    first = wd.write_backup(tmp_path, "dash-a", {"title": "A"}, NOW)
    second = wd.write_backup(tmp_path, "dash-a", {"title": "B"}, NOW)

    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert wd.read_backup(first) == ("dash-a", {"title": "A"})
    assert wd.read_backup(second) == ("dash-a", {"title": "B"})


def test_foreign_json_is_not_a_backup(tmp_path):
    path = tmp_path / "other.json"
    path.write_text('{"title": "A"}')

    with pytest.raises(wd.ConfigError, match="not a wall display backup"):
        wd.read_backup(path)


# --------------------------------------------------------------- saving --
def test_deploy_saves_changes_after_backing_up_every_display(
    two_displays, tmp_path
):
    client = FakeDashboards(two_displays)
    configs = {**two_displays, "dash-b": {"title": "B2", "views": []}}

    saved = asyncio.run(wd.deploy(client, configs, tmp_path, yes))

    assert saved == ["dash-b"]
    assert client.saves == ["dash-b"]
    assert client.stored["dash-b"]["title"] == "B2"
    backups = sorted(wd.read_backup(p) for p in tmp_path.iterdir())
    assert backups == sorted(two_displays.items())


def test_deploy_is_idempotent(two_displays, tmp_path):
    client = FakeDashboards(two_displays)

    saved = asyncio.run(wd.deploy(client, two_displays, tmp_path, yes))

    assert saved == []
    assert client.saves == []
    assert not list(tmp_path.iterdir())


def test_deploy_only_given_display_still_backs_up_both(two_displays, tmp_path):
    client = FakeDashboards(two_displays)

    asyncio.run(wd.deploy(client, {"dash-a": {"title": "A2"}}, tmp_path, yes))

    assert client.saves == ["dash-a"]
    assert len(list(tmp_path.iterdir())) == 2


def test_cancelled_deploy_saves_nothing(two_displays, tmp_path):
    client = FakeDashboards(two_displays)

    with pytest.raises(wd.DeployError, match="cancelled"):
        asyncio.run(
            wd.deploy(
                client, {"dash-a": {"title": "A2"}}, tmp_path, lambda _q: False
            )
        )
    assert client.saves == []
    assert not list(tmp_path.iterdir())


def test_missing_dashboard_fails(two_displays, tmp_path):
    client = FakeDashboards(two_displays)

    with pytest.raises(wd.DeployError, match="no storage dashboard dash-c"):
        asyncio.run(
            wd.deploy(client, {"dash-c": {"title": "C"}}, tmp_path, yes)
        )
    assert client.saves == []


def test_failed_check_after_saving_names_the_backup(two_displays, tmp_path):
    client = FakeDashboards(two_displays, mangle=True)

    with pytest.raises(wd.DeployError, match="differs after saving; restore"):
        asyncio.run(
            wd.deploy(client, {"dash-a": {"title": "A2"}}, tmp_path, yes)
        )


def test_restore_backs_up_the_current_state_first(two_displays, tmp_path):
    backup = wd.write_backup(tmp_path / "old", "dash-a", {"title": "A0"}, NOW)
    client = FakeDashboards(two_displays)

    assert asyncio.run(wd.restore(client, backup, tmp_path / "new", yes))
    assert client.stored["dash-a"] == {"title": "A0"}
    backups = sorted(wd.read_backup(p) for p in (tmp_path / "new").iterdir())
    assert backups == sorted(two_displays.items())
    assert not asyncio.run(wd.restore(client, backup, tmp_path / "new", yes))


# ---------------------------------------------------------- credentials --
def test_env_file_fills_in_what_the_environment_lacks(tmp_path, monkeypatch):
    env = tmp_path / "ha.env"
    env.write_text(
        "# HA\nexport HA_URL='http://ha.test:8123'\nHA_TOKEN=\"file\"\n"
    )
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.setenv("HA_TOKEN", "environment")

    assert wd.credentials(env) == ("http://ha.test:8123", "environment")


def test_missing_credentials_fail(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)

    with pytest.raises(wd.ConfigError, match="HA_URL and HA_TOKEN"):
        wd.credentials(None)
