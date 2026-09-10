"""Android user profiles over adb.

Setting a profile up by hand is the slow part: create it, switch to it, then wait
while every app downloads again. ``pm install-existing`` avoids that last part
entirely — the APK is already on the device, so adding it to another profile is a
link, not a download — which is what turns ten minutes of tapping into one
command.

Everything that parses ``pm`` output is pure and tested. The rest takes a
``run`` callable so it can be driven by a fake.

Arguments here reach a shell on the device, and they arrive from a model, so
names and package names are allowlisted rather than escaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# UserInfo{11:Work profile:1030} running
USER_LINE = re.compile(
    r"UserInfo\{(?P<id>\d+):(?P<name>[^:]*):(?P<flags>[0-9a-fA-F]+)\}(?P<rest>.*)"
)
PACKAGE_LINE = re.compile(r"^package:(?P<name>\S+)")
CREATED_ID = re.compile(r"id\s+(?P<id>\d+)")

# A profile name becomes a shell argument; keep it to something unambiguous.
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")
PACKAGE_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

OWNER_ID = 0

# android.content.pm.UserInfo, the flags worth naming.
FLAGS = {
    0x1: "primary",
    0x2: "admin",
    0x4: "guest",
    0x8: "restricted",
    0x10: "initialized",
    0x20: "managed-profile",
    0x40: "disabled",
    0x100: "ephemeral",
    0x400: "full",
    0x800: "system",
}


class UserError(RuntimeError):
    """A profile operation that could not be carried out."""


def check_name(name: str) -> str:
    if not NAME_OK.match(name or ""):
        raise UserError(
            f"{name!r} is not a usable profile name. Use letters, digits, "
            "spaces, hyphens and underscores, up to 32 characters."
        )
    return name


def check_package(package: str) -> str:
    if not PACKAGE_OK.match(package or ""):
        raise UserError(
            f"{package!r} does not look like a package name "
            "(expected something like com.example.app)."
        )
    return package


@dataclass(frozen=True)
class UserInfo:
    id: int
    name: str
    flags: int
    running: bool = False

    @property
    def is_owner(self) -> bool:
        return self.id == OWNER_ID

    def flag_names(self) -> list[str]:
        return [label for bit, label in sorted(FLAGS.items()) if self.flags & bit]

    def render(self) -> str:
        bits = [f"{self.id}", self.name or "(unnamed)"]
        if self.running:
            bits.append("running")
        names = [f for f in self.flag_names() if f != "initialized"]
        if names:
            bits.append(", ".join(names))
        return "  ".join(bits)


def parse_users(output: str) -> list[UserInfo]:
    """Parse ``pm list users``.

    ::

        Users:
            UserInfo{0:Owner:c13} running
            UserInfo{11:Work:1030}
    """
    users = []
    for line in (output or "").splitlines():
        match = USER_LINE.search(line)
        if not match:
            continue
        users.append(
            UserInfo(
                id=int(match.group("id")),
                name=match.group("name").strip(),
                flags=int(match.group("flags"), 16),
                running="running" in match.group("rest"),
            )
        )
    return users


def parse_packages(output: str) -> list[str]:
    """Parse ``pm list packages``."""
    found = []
    for line in (output or "").splitlines():
        match = PACKAGE_LINE.match(line.strip())
        if match:
            found.append(match.group("name"))
    return sorted(found)


def parse_created_id(output: str) -> int:
    """Pull the new id out of ``Success: created user id 11``."""
    match = CREATED_ID.search(output or "")
    if not match:
        raise UserError(
            "could not tell which profile was created from: "
            f"{(output or '').strip() or '(no output)'}"
        )
    return int(match.group("id"))


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def list_users(run) -> list[UserInfo]:
    return parse_users(run("pm", "list", "users"))


def packages_for(run, user_id: int, third_party_only: bool = True) -> list[str]:
    args = ["pm", "list", "packages", "--user", str(int(user_id))]
    if third_party_only:
        args.append("-3")  # skip the ~200 system packages
    return parse_packages(run(*args))


def _max_users_hint(run) -> str:
    """The device's profile cap, if it will tell us.

    Hitting the cap is the usual reason a create fails, so it is worth saying.
    This is a diagnostic and must never become the reported failure itself, so
    anything going wrong here yields no hint rather than an error.
    """
    try:
        reported = run("pm", "get-max-users").strip()
    except Exception:  # noqa: BLE001 - see above
        return ""
    return f" The device reports: {reported}." if reported else ""


def create_user(run, name: str) -> int:
    """Create a profile and return its id."""
    check_name(name)
    output = run("pm", "create-user", name)
    if "Success" not in output:
        raise UserError(
            f"could not create {name!r}: {output.strip() or '(no output)'}."
            f"{_max_users_hint(run)}"
        )
    return parse_created_id(output)


def remove_user(run, user_id: int) -> None:
    """Remove a profile.

    The owner is refused: removing user 0 is not a profile operation, it is
    wiping the device's main account.
    """
    user_id = int(user_id)
    if user_id == OWNER_ID:
        raise UserError(
            "refusing to remove user 0, the device owner. Removing it is not a "
            "profile operation; it takes the main account with it."
        )
    output = run("pm", "remove-user", str(user_id))
    if "Success" not in output:
        raise UserError(
            f"could not remove profile {user_id}: "
            f"{output.strip() or '(no output)'}"
        )


def switch_user(run, user_id: int) -> None:
    run("am", "switch-user", str(int(user_id)))


def install_existing(run, user_id: int, package: str) -> None:
    """Add an already-downloaded app to a profile.

    This is the whole reason provisioning is fast: the APK is on the device
    already, so nothing is downloaded.
    """
    check_package(package)
    output = run(
        "pm", "install-existing", "--user", str(int(user_id)), package
    )
    if "installed for user" not in output and "Success" not in output:
        raise UserError(
            f"could not add {package} to profile {user_id}: "
            f"{output.strip() or '(no output)'}"
        )


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------


@dataclass
class ProvisionResult:
    user_id: int
    name: str
    installed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    switched: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        lines = [f"profile {self.name!r} created as user {self.user_id}"]
        if self.installed:
            lines.append(f"added {len(self.installed)} apps: {', '.join(self.installed)}")
        if self.failed:
            lines.append("could not add:")
            lines.extend(f"  {pkg}: {why}" for pkg, why in self.failed.items())
        if self.switched:
            lines.append(f"switched to user {self.user_id}")
        else:
            lines.append(
                f"still on the previous profile; switch with "
                f"switch_user(user_id={self.user_id})"
            )
        return "\n".join(lines)


def provision(
    run,
    name: str,
    packages: list[str] | None = None,
    *,
    switch: bool = False,
) -> ProvisionResult:
    """Create a profile and add apps to it in one go.

    Every package is validated before anything is created, so a typo fails
    before a half-built profile is left behind. A package that cannot be added
    is reported rather than aborting the rest: one missing app is not a reason
    to throw away a profile that is otherwise ready.
    """
    check_name(name)
    wanted = [check_package(p) for p in (packages or [])]

    user_id = create_user(run, name)
    result = ProvisionResult(user_id=user_id, name=name)

    for package in wanted:
        try:
            install_existing(run, user_id, package)
            result.installed.append(package)
        except UserError as exc:
            result.failed[package] = str(exc)

    if switch:
        switch_user(run, user_id)
        result.switched = True
    return result
