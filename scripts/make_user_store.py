"""Build a production user store, and materialise one inside a container.

WHY THIS EXISTS

`data/users.json` ships six accounts and every one of them carries `"demo":
true`, so `APP_ENV=production` refuses to start with it (auth.py's
`validate_production_config`). That refusal is correct -- those passwords are
published in this repository and on the sign-in screen -- but it means a
production deployment needs a user store that does not exist yet, and there is
nowhere good to put one:

  * committing it puts password hashes in git;
  * baking it into an image puts them in a layer anyone who pulls it can read;
  * a container filesystem does not survive a restart, so writing one by hand
    is undone by the next deploy.

So the store travels as an environment variable like every other secret, and
this script is the two halves of that:

    GENERATE (run locally, once)

        python scripts/make_user_store.py generate --user alice:admin

        Prompts for each password, hashes it with the SAME `auth.hash_password`
        the application verifies against, and prints one line of JSON to paste
        into the deployment's AUTH_USERS_JSON variable. No plaintext password
        is written anywhere, and nothing is stored by this script.

    RENDER (run by the container, at startup)

        python scripts/make_user_store.py render

        Writes $AUTH_USERS_JSON to $AUTH_USERS_FILE so auth.load_users() can
        read it. A no-op when AUTH_USERS_JSON is unset, which is what keeps
        local development -- where the demo store is the point -- unchanged.

THIS SCRIPT IS NOT PART OF THE APPLICATION. It writes a file and exits;
`auth.py` is untouched and still reads exactly what it always read.
"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import auth  # noqa: E402  (path set above so backend modules import as top level)

DEFAULT_TARGET = "/tmp/users.json"

# Roles the token issuer actually knows about. Checked here so a typo is caught
# while a human is watching, rather than becoming an account that authenticates
# and then holds no scope at all.
KNOWN_ROLES = sorted(auth.ROLE_SCOPES)


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def generate(argv):
    """Print a users.json suitable for AUTH_USERS_JSON. Passwords are prompted."""
    specs = []
    i = 0
    while i < len(argv):
        if argv[i] == "--user":
            i += 1
            if i >= len(argv):
                _fail("--user needs a value, e.g. --user alice:admin")
            specs.append(argv[i])
        else:
            _fail(f"unrecognised argument {argv[i]!r}")
        i += 1

    if not specs:
        _fail("no accounts. Use --user NAME:ROLE[,ROLE] at least once, e.g.\n"
              "  --user alice:admin --user bob:reviewer\n"
              f"known roles: {', '.join(KNOWN_ROLES)}")

    rows = []
    seen = set()
    for spec in specs:
        name, _, roles_csv = spec.partition(":")
        name = name.strip()
        roles = [r.strip() for r in roles_csv.split(",") if r.strip()]
        if not name:
            _fail(f"{spec!r} has no username")
        if name in seen:
            _fail(f"{name!r} listed twice")
        seen.add(name)
        if not roles:
            _fail(f"{name!r} has no role. Use NAME:ROLE, e.g. {name}:reviewer")
        for role in roles:
            if role not in KNOWN_ROLES:
                _fail(f"{role!r} is not a role this application grants. "
                      f"Known roles: {', '.join(KNOWN_ROLES)}")

        password = getpass.getpass(f"password for {name}: ")
        again = getpass.getpass(f"repeat password for {name}: ")
        if password != again:
            _fail(f"the two passwords for {name!r} did not match")
        if len(password) < 12:
            _fail(f"the password for {name!r} is shorter than 12 characters. "
                  f"This is the only credential in front of the API.")

        row = {"username": name, "roles": roles,
               "password_hash": auth.hash_password(password)}

        # A client account needs its vendor binding, and one without it is
        # refused at request time (Phase J) -- which is correct, and is a
        # confusing way to find out. Ask now instead.
        if any(r.startswith("client") for r in roles):
            client_id = input(f"  client_id for {name} (e.g. C-ACME): ").strip()
            client_name = input(f"  client_name for {name}: ").strip()
            vendor_ids = [v.strip() for v in
                          input(f"  vendor_ids for {name}, comma separated: ").split(",")
                          if v.strip()]
            if not client_id or not vendor_ids:
                _fail(f"{name!r} is a client account, so it needs a client_id and at "
                      f"least one vendor_id. Without them the portal shows it nothing.")
            row.update({"client_id": client_id, "client_name": client_name or client_id,
                        "vendor_ids": vendor_ids})

        # Deliberately NO "demo" key. That flag is what the production start-up
        # check refuses, and setting it here would recreate the exact problem
        # this script exists to solve.
        rows.append(row)

    print("\n--- paste this whole line as AUTH_USERS_JSON ---\n", file=sys.stderr)
    print(json.dumps(rows, separators=(",", ":")))
    print("\n--- the container writes it to AUTH_USERS_FILE (default /tmp/users.json) ---",
          file=sys.stderr)
    return 0


def render():
    """Write $AUTH_USERS_JSON to $AUTH_USERS_FILE. A no-op when unset."""
    raw = os.environ.get("AUTH_USERS_JSON", "").strip()
    if not raw:
        # Not an error: a development container legitimately wants the demo
        # store, and APP_ENV=production refuses that on its own with a far
        # better message than this script could give.
        print("[user-store] AUTH_USERS_JSON not set; using the store on disk.",
              file=sys.stderr)
        return 0

    try:
        rows = json.loads(raw)
    except ValueError as exc:
        _fail(f"AUTH_USERS_JSON is not valid JSON ({exc}).")

    if not isinstance(rows, list) or not rows:
        _fail("AUTH_USERS_JSON must be a non-empty JSON array of user records.")
    for row in rows:
        if not isinstance(row, dict) or not row.get("username"):
            _fail("every AUTH_USERS_JSON record needs a username.")
        if not row.get("password_hash"):
            _fail(f"{row.get('username')!r} has no password_hash. Generate the store "
                  f"with: python scripts/make_user_store.py generate --user ...")

    target = os.environ.get("AUTH_USERS_FILE", DEFAULT_TARGET)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    try:
        os.chmod(target, 0o600)   # best effort; not every filesystem honours it
    except OSError:
        pass

    # Names only. A hash is not a password, but it is not something to print
    # into a deployment log either.
    print(f"[user-store] wrote {len(rows)} account(s) to {target}: "
          f"{', '.join(sorted(r['username'] for r in rows))}", file=sys.stderr)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] == "generate":
        return generate(argv[1:])
    if argv[0] == "render":
        return render()
    _fail(f"unknown command {argv[0]!r}. Use 'generate' or 'render'.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
