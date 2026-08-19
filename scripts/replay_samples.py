"""Drive the seven sample invoices through the running API, in manifest order.

Order is load-bearing. Several samples are about the state earlier ones leave
behind: the split-PO story only works as 02 -> 03 -> 03b, and 06 is only a
duplicate because 01 ran first. Each verdict is checked against
sample_invoices/manifest.json, so this doubles as a check that a fresh database
still reproduces the documented behaviour.

Used by reset-demo.ps1 -Replay; safe to run on its own against a live server.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "sample_invoices")

USERNAME = os.environ.get("DEMO_USER", "analyst")
PASSWORD = os.environ.get("DEMO_PASS", "demo-analyst")


def get_token() -> str:
    body = f"username={USERNAME}&password={PASSWORD}".encode()
    req = urllib.request.Request(
        f"{BASE}/api/auth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"Could not sign in as {USERNAME} (HTTP {e.code}). "
            "Is the server running, and are the demo accounts present?"
        )
    except OSError:
        raise SystemExit(f"Could not reach the API at {BASE}. Is the server running?")


def process(token: str, filename: str) -> dict | None:
    """POST the PDF to the streaming endpoint and return the final result."""
    with open(os.path.join(SAMPLES, filename), "rb") as f:
        pdf = f.read()

    boundary = "----replay-samples-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BASE}/api/runs/stream",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    # The response is SSE; the last "final" frame carries the verdict.
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read().decode("utf-8", "replace")

    result = None
    for line in raw.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event.get("type") == "final":
                result = event["result"]
    return result


def main() -> int:
    with open(os.path.join(SAMPLES, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    ordered = sorted(manifest.items(), key=lambda kv: kv[1]["order"])
    token = get_token()
    failures = []

    print(f"{'#':<3}{'sample':<38}{'expected':<26}{'got':<14}")
    print("-" * 84)

    for i, (filename, meta) in enumerate(ordered, 1):
        result = process(token, filename)
        if result is None:
            print(f"{i:<3}{filename:<38}{'-':<26}{'no result':<14}FAIL")
            failures.append(filename)
            continue

        got = result["status"]
        expected = meta["expect"]
        # Sample 05 is route-dependent by design: with the vision route
        # reachable it is read and approved; without it, nothing is guessed at
        # and it goes to a human. Both are correct.
        alt = meta.get("expect_with_vision")
        ok = got == expected or (alt is not None and got == alt)
        shown = expected if alt is None else f"{expected} or {alt}"

        print(f"{i:<3}{filename:<38}{shown:<26}{got:<14}{'' if ok else 'FAIL'}")
        if not ok:
            failures.append(filename)
            for reason in result.get("reasons", []):
                text = reason if isinstance(reason, str) else reason.get("text", "")
                level = "info" if isinstance(reason, str) else reason.get("level", "info")
                if level == "fail":
                    print(f"      {text}")

    print()
    if failures:
        print(f"{len(failures)} sample(s) did not match the manifest: {', '.join(failures)}")
        return 1

    print("All seven samples match the manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
