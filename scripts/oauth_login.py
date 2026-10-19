"""Create an OAuth authorized-user token for the Google Docs API.

Only needed if you cannot use a service account (e.g. your organisation blocks key
creation). Run it on a machine with a browser, then copy the output file to the server:

    pip install -e ".[oauth]"
    python scripts/oauth_login.py client_secret.json secrets/google-credentials.json

On a headless server, add "--port 8765 --no-browser", forward that port to the machine that
has your browser (ssh -L 8765:localhost:8765 user@server, or the VS Code Ports panel), and open
the printed URL there.

client_secret.json is an "OAuth client ID" of type Desktop app from Google Cloud Console.
If your Google Cloud project belongs to a Workspace organisation, set the consent screen user
type to "Internal": no verification and no token expiry. Otherwise ("External") publish it
("In production"); in "Testing" mode Google expires the refresh token after 7 days.
"""

import argparse
import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/documents"]
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"  # only files this app created
DRIVE_FULL_SCOPE = "https://www.googleapis.com/auth/drive"  # all of the user's Drive
PAGE = b"<html><body><h3>Authorization received. You can close this tab.</h3></body></html>"


def _bind(port: int) -> HTTPServer:
    """Bind the redirect listener, waiting out a lingering TIME_WAIT on a reused port."""
    deadline = time.monotonic() + 90
    while True:
        try:
            return HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError:
            if port == 0 or time.monotonic() > deadline:
                raise
            time.sleep(2)


def wait_for_redirect(server: HTTPServer, state: str) -> dict[str, list[str]]:
    """Serve requests until the real OAuth redirect (matching state) arrives.

    Unlike google-auth-oauthlib's run_local_server this ignores stray requests (port probes,
    favicon, a forwarded-port health check) instead of failing on the first one.
    """
    result: dict[str, list[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state") == [state] and ("code" in query or "error" in query):
                result.update(query)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(PAGE)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    while not result:
        server.handle_request()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a Google Docs OAuth token file.")
    ap.add_argument("client_secret")
    ap.add_argument("out")
    ap.add_argument("--port", type=int, default=0, help="local port for the OAuth redirect (0 = any)")
    ap.add_argument("--no-browser", action="store_true", help="only print the URL (headless server)")
    ap.add_argument("--drive", action="store_true", help="also request drive.file (rename/move the Docs this app created)")
    ap.add_argument("--drive-full", action="store_true", help="also request full Drive access (move Docs into existing folders)")
    args = ap.parse_args()

    scopes = list(SCOPES)
    if args.drive_full:
        scopes.append(DRIVE_FULL_SCOPE)
    elif args.drive:
        scopes.append(DRIVE_FILE_SCOPE)
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, scopes)
    server = _bind(args.port)
    flow.redirect_uri = f"http://localhost:{server.server_port}/"
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    print("Open this URL in your browser and approve access:\n" + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    query = wait_for_redirect(server, state)
    server.server_close()
    if "error" in query:
        print(f"Google returned an error: {query['error'][0]}")
        return 1
    flow.fetch_token(code=query["code"][0])

    data = json.loads(flow.credentials.to_json())
    data["type"] = "authorized_user"
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(args.out, 0o600)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
