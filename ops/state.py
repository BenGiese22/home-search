"""Move the Compass session between this laptop and the private Blob store.

    python ops/state.py push          # laptop -> Blob
    python ops/state.py pull --force  # Blob -> laptop

Only ever run by hand. In steady state nothing calls this: the launcher seeds
a cold sandbox from Blob and the reaper collects the refreshed session back
into it, both from inside Vercel.

What it is for is the first run and the bad day. Seeding the store before the
first canary is what lets that canary start warm instead of driving Compass's
login form -- and a cold login is the one step in the whole pipeline that a
human might have to babysit. `pull` is the other direction: recovering a
working session onto the laptop after the sandbox has been the only thing
holding a good one.

Uses BLOB_STATE_READ_WRITE_TOKEN, which points at the PRIVATE store. A
Compass session behind a public URL is a credential anyone holding the link
can replay, and private access is a store-level setting, so it cannot share
the store that serves listing photos.
"""

import sys
from pathlib import Path

import requests

from src.blob_state import get_state, put_state
from src.config import load_env

STATE_NAME = "compass_state.json"
LOCAL_PATH = Path("data") / ".auth" / STATE_NAME

TOKEN_VAR = "BLOB_STATE_READ_WRITE_TOKEN"


def push(token: str, local: Path = LOCAL_PATH, put=requests.put) -> str:
    """Upload the local session, replacing whatever is stored."""
    if not local.exists():
        raise FileNotFoundError(
            f"no session at {local}. Run a scrape first -- the session is "
            "written as a side effect of logging in."
        )
    data = local.read_bytes()
    if not data:
        raise ValueError(f"{local} is empty; refusing to push it over a good session")
    url = put_state(STATE_NAME, data, token, put=put)
    print(f"pushed {len(data)} bytes to state/{STATE_NAME}")
    return url


def pull(
    token: str, local: Path = LOCAL_PATH, force: bool = False, get=requests.get
) -> bytes:
    """Download the stored session onto the laptop.

    Refuses to land on an existing file without --force. There is no reliable
    way to tell which of the two is newer -- the store returns bytes, not a
    version -- so the safe default is to never silently replace a session that
    might be the working one. Being told to re-run with --force costs seconds;
    clobbering a good session costs a login.
    """
    if local.exists() and not force:
        raise FileExistsError(
            f"{local} already exists. Re-run with --force to replace it "
            "(there is no way to tell which copy is newer, so this is your call)."
        )
    data = get_state(STATE_NAME, token, get=get)
    if data is None:
        raise FileNotFoundError(
            f"nothing stored at state/{STATE_NAME}; push one first"
        )
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    print(f"pulled {len(data)} bytes into {local}")
    return data


def main(argv: list[str] | None = None, env=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    if command not in {"push", "pull"}:
        print("usage: state.py {push|pull} [--force]", file=sys.stderr)
        return 2

    env = load_env() if env is None else env
    token = (env.get(TOKEN_VAR) or "").strip()
    if not token:
        # Names the variable, never a value.
        print(f"{TOKEN_VAR} is not set in .env", file=sys.stderr)
        return 1

    try:
        if command == "push":
            push(token)
        else:
            pull(token, force="--force" in args)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
