"""CLI for the single-user account - run it inside the container:

    docker exec -it homestew python -m homestew.auth_cli reset-password
    docker exec -it homestew python -m homestew.auth_cli status
    docker exec -it homestew python -m homestew.auth_cli clear-password

This is the documented recovery path when the password is forgotten: it
writes straight to the same settings file the app uses (no server needed),
and because session cookies are signed with a key derived from the password,
resetting it invalidates every logged-in browser automatically.

Non-interactive use (CI/scripts): set HOMESTEW_PASSWORD and pipe nothing -
the tool reads it from the environment instead of prompting.
"""
import argparse
import getpass
import os
import sys

from homestew.services import auth


def _read_new_password() -> str:
    """Prompt twice with echo hidden, or take HOMESTEW_PASSWORD verbatim."""
    env_password = os.environ.get("HOMESTEW_PASSWORD")
    if env_password is not None:
        return env_password
    if not sys.stdin or not sys.stdin.isatty():
        print(
            "No interactive terminal. Run with -it, or set HOMESTEW_PASSWORD.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    while True:
        first = getpass.getpass("New HomeStew password: ")
        if len(first) < 8:
            print("Password must be at least 8 characters. Try again.")
            continue
        second = getpass.getpass("Repeat the new password: ")
        if first != second:
            print("Passwords do not match. Try again.")
            continue
        return first


def cmd_status() -> int:
    configured = auth.password_configured()
    print(f"Password configured: {configured}")
    return 0


def cmd_reset_password() -> int:
    had = auth.password_configured()
    password = _read_new_password()
    auth.set_password(password)
    print(
        "Password {} successfully. All existing sessions are now invalid - "
        "sign in again in the browser.".format("updated" if had else "created")
    )
    return 0


def cmd_clear_password() -> int:
    if not auth.password_configured():
        print("There is no password to clear.")
        return 1
    auth.clear_password()
    print(
        "Password cleared. The next page load will show the create-account "
        "screen (authentication stays disabled until one is created)."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m homestew.auth_cli",
        description="HomeStew single-user account management.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show whether a password is configured")
    sub.add_parser(
        "reset-password",
        help="Set a new password (prompts twice; HOMESTEW_PASSWORD for scripts)",
    )
    sub.add_parser(
        "clear-password",
        help="Remove the password and re-arm first-run account creation",
    )
    args = parser.parse_args(argv)

    handlers = {
        "status": cmd_status,
        "reset-password": cmd_reset_password,
        "clear-password": cmd_clear_password,
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
