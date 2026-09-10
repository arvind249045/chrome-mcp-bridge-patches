"""Tests for profile parsing and provisioning. No device required."""

import pytest

from android_mcp.users import (
    UserError,
    UserInfo,
    check_package,
    create_user,
    install_existing,
    list_users,
    packages_for,
    parse_created_id,
    parse_packages,
    parse_users,
    provision,
    remove_user,
    switch_user,
)

PM_LIST_USERS = """Users:
\tUserInfo{0:Owner:c13} running
\tUserInfo{11:Work profile:1030}
\tUserInfo{12:Testing:410} running
"""


class FakeShell:
    """A scripted `adb shell`, recording what it was asked to run."""

    def __init__(self, replies=None):
        self.calls: list[tuple[str, ...]] = []
        self.replies = dict(replies or {})

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        for prefix, reply in self.replies.items():
            if " ".join(args).startswith(prefix):
                return reply(*args) if callable(reply) else reply
        return ""

    def ran(self, prefix: str) -> bool:
        return any(" ".join(c).startswith(prefix) for c in self.calls)


def shell(**kw):
    defaults = {
        "pm list users": PM_LIST_USERS,
        "pm create-user": "Success: created user id 13",
        "pm remove-user": "Success: removed user",
        "pm install-existing": "Package com.bistro.app installed for user: 13",
    }
    defaults.update(kw)
    return FakeShell(defaults)


# --- parsing -------------------------------------------------------------


def test_parses_every_profile():
    users = parse_users(PM_LIST_USERS)
    assert [(u.id, u.name) for u in users] == [
        (0, "Owner"),
        (11, "Work profile"),
        (12, "Testing"),
    ]


def test_parses_running_state():
    users = {u.id: u for u in parse_users(PM_LIST_USERS)}
    assert users[0].running and users[12].running
    assert not users[11].running


def test_parses_hex_flags_into_names():
    users = {u.id: u for u in parse_users(PM_LIST_USERS)}
    assert "primary" in users[0].flag_names()
    assert "managed-profile" in users[11].flag_names()


def test_identifies_the_owner():
    users = {u.id: u for u in parse_users(PM_LIST_USERS)}
    assert users[0].is_owner and not users[11].is_owner


def test_handles_empty_and_junk_output():
    assert parse_users("") == []
    assert parse_users("Users:\n") == []
    assert parse_users("something unexpected") == []


def test_renders_a_profile_readably():
    rendered = UserInfo(11, "Work", 0x1030, running=False).render()
    assert "11" in rendered and "Work" in rendered


def test_parses_package_lists():
    assert parse_packages("package:com.b\npackage:com.a\n") == ["com.a", "com.b"]
    assert parse_packages("") == []


def test_parses_the_new_profile_id():
    assert parse_created_id("Success: created user id 13") == 13


def test_an_unparseable_create_response_is_an_error():
    with pytest.raises(UserError) as exc:
        parse_created_id("Success")
    assert "could not tell which profile" in str(exc.value)


# --- validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "a;rm -rf /", "name$(whoami)", "`id`", "a" * 33, " leading", "semi;colon"],
)
def test_bad_profile_names_are_refused(bad):
    """These become shell arguments on the device, and arrive from a model."""
    with pytest.raises(UserError):
        create_user(shell(), bad)


@pytest.mark.parametrize(
    "bad", ["", "not-a-package", "com", "com.a;rm -rf /", "$(id)", "../etc"]
)
def test_bad_package_names_are_refused(bad):
    with pytest.raises(UserError):
        check_package(bad)


@pytest.mark.parametrize("good", ["com.bistro.app", "com.a.b.c", "io.github.x_y"])
def test_good_package_names_pass(good):
    assert check_package(good) == good


def test_a_refused_name_never_reaches_the_device():
    fake = shell()
    with pytest.raises(UserError):
        create_user(fake, "bad;name")
    assert fake.calls == []


# --- operations ----------------------------------------------------------


def test_listing_profiles():
    assert len(list_users(shell())) == 3


def test_creating_a_profile_returns_its_id():
    fake = shell()
    assert create_user(fake, "Testing") == 13
    assert ("pm", "create-user", "Testing") in fake.calls


def test_a_failed_create_reports_the_limit():
    """Hitting the max-users cap is the usual cause, so say so."""
    fake = shell(
        **{
            "pm create-user": "Error: couldn't create User.",
            "pm get-max-users": "Maximum supported users: 4",
        }
    )
    with pytest.raises(UserError) as exc:
        create_user(fake, "Testing")
    assert "Maximum supported users: 4" in str(exc.value)


def test_listing_a_profiles_apps_skips_system_packages():
    fake = shell(**{"pm list packages": "package:com.bistro.app"})
    packages_for(fake, 11)
    assert ("pm", "list", "packages", "--user", "11", "-3") in fake.calls


def test_removing_the_owner_is_refused():
    """Removing user 0 takes the device's main account with it."""
    fake = shell()
    with pytest.raises(UserError) as exc:
        remove_user(fake, 0)
    assert "device owner" in str(exc.value)
    assert fake.calls == []


def test_removing_a_profile_works():
    fake = shell()
    remove_user(fake, 11)
    assert ("pm", "remove-user", "11") in fake.calls


def test_a_failed_remove_is_reported():
    fake = shell(**{"pm remove-user": "Error: couldn't remove user"})
    with pytest.raises(UserError):
        remove_user(fake, 11)


def test_switching_profiles():
    fake = shell()
    switch_user(fake, 11)
    assert ("am", "switch-user", "11") in fake.calls


def test_install_existing_does_not_download():
    """The APK is already on the device; this only adds it to the profile."""
    fake = shell()
    install_existing(fake, 13, "com.bistro.app")
    assert ("pm", "install-existing", "--user", "13", "com.bistro.app") in fake.calls


def test_a_failed_install_is_reported():
    fake = shell(**{"pm install-existing": "Failure [not installed for 0]"})
    with pytest.raises(UserError) as exc:
        install_existing(fake, 13, "com.missing.app")
    assert "could not add com.missing.app" in str(exc.value)


# --- provisioning --------------------------------------------------------


def test_provisioning_creates_the_profile_and_adds_the_apps():
    fake = shell()
    result = provision(fake, "Testing", ["com.bistro.app", "com.fampay.app"])
    assert result.ok
    assert result.user_id == 13
    assert result.installed == ["com.bistro.app", "com.fampay.app"]
    assert fake.ran("pm create-user")


def test_provisioning_does_not_switch_unless_asked():
    fake = shell()
    result = provision(fake, "Testing", ["com.bistro.app"])
    assert not result.switched
    assert not fake.ran("am switch-user")
    assert "switch_user" in result.render()


def test_provisioning_can_switch():
    fake = shell()
    result = provision(fake, "Testing", [], switch=True)
    assert result.switched and fake.ran("am switch-user")


def test_a_bad_package_fails_before_the_profile_is_created():
    """Otherwise a typo leaves a half-built profile behind."""
    fake = shell()
    with pytest.raises(UserError):
        provision(fake, "Testing", ["com.bistro.app", "nonsense"])
    assert not fake.ran("pm create-user")


def test_one_missing_app_does_not_throw_away_the_profile():
    def install(*args):
        if "com.missing.app" in args:
            return "Failure [not installed for 0]"
        return "installed for user: 13"

    fake = shell(**{"pm install-existing": install})
    result = provision(fake, "Testing", ["com.bistro.app", "com.missing.app"])
    assert not result.ok
    assert result.installed == ["com.bistro.app"]
    assert "com.missing.app" in result.failed
    assert "could not add" in result.render()


def test_provisioning_with_no_apps_is_fine():
    result = provision(shell(), "Empty")
    assert result.ok and result.installed == []
