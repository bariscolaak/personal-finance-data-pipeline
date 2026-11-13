"""Minimal local OAuth web app for Česká spořitelna."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
RAW_DATA_ROOT = ROOT / "DataBase" / "raw_json"
SESSION = {"state": None, "access_token": None, "refresh_token": None}

## READS .ENV FILE AND SETS ENVIRONMENT VARIABLES
def load_env() -> None:
    for raw_line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()





### READS ENVIRONMENT VARIABLES AND RAISES ERROR IF NOT SET
def setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in {ROOT / '.env'}")
    return value






def assert_environment_url(url: str) -> None:
    parsed = urlparse(url)
    environment = setting("ERSTE_ENVIRONMENT").lower()
    if environment == "sandbox":
        valid = parsed.hostname == "webapi.developers.erstegroup.com" and "/sandbox/" in url
    elif environment == "production":
        valid = parsed.hostname in {"www.csas.cz", "bezpecnost.csas.cz"} and "/sandbox/" not in url
    else:
        raise ValueError("ERSTE_ENVIRONMENT must be 'sandbox' or 'production'")
    if parsed.scheme != "https" or not valid:
        raise ValueError(f"Refusing URL that does not match {environment}: {url}")






## THIS CRETS URL FOR THE USER TO LOGIN AND AUTHORIZE THE APP
def authorization_url() -> str:
    auth_url = setting("ERSTE_AUTH_URL")
    assert_environment_url(auth_url)
    state = secrets.token_urlsafe(32)
    SESSION["state"] = state
    params = {
        "response_type": "code",
        "client_id": setting("ERSTE_CLIENT_ID"),
        "redirect_uri": setting("ERSTE_REDIRECT_URI"),
        "state": state,
    }
    scope = os.environ.get("ERSTE_SCOPE", "").strip()
    if scope:
        params["scope"] = scope
    return f"{auth_url}?{urllib.parse.urlencode(params)}"




## EXCHANGES THE AUTHORIZATION CODE FOR AN ACCESS TOKEN AND REFRESH TOKEN
def exchange_code(code: str) -> dict:
    token_url = setting("ERSTE_TOKEN_URL")
    assert_environment_url(token_url)
    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": setting("ERSTE_REDIRECT_URI"),
            "client_id": setting("ERSTE_CLIENT_ID"),
            "client_secret": setting("ERSTE_CLIENT_SECRET"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    return request_json(request)





def api_get(path: str, query: dict[str, str] | None = None) -> dict | list:
    base = setting("ERSTE_BASE_URL").rstrip("/")
    assert_environment_url(base)
    url = f"{base}/{path.strip('/')}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            os.environ.get("ERSTE_API_KEY_HEADER", "web-api-key"): setting("ERSTE_API_KEY"),
            "Authorization": f"Bearer {SESSION['access_token']}",
        },
    )
    return request_json(request)


def get_accounts() -> dict | list:
    return api_get(os.environ.get("ERSTE_ACCOUNTS_PATH", "my/accounts"))


def get_balance(account_id: str) -> dict | list:
    return api_get(f"my/accounts/{urllib.parse.quote(account_id, safe='')}/balance")


def get_transactions(account_id: str) -> dict:
    today = date.today()
    try:
        start_date = today.replace(year=today.year - 3)
    except ValueError:
        # February 29 has no equivalent in a non-leap year.
        start_date = today.replace(year=today.year - 3, day=28)
    path = f"my/accounts/{urllib.parse.quote(account_id, safe='')}/transactions"
    query = {
        "fromDate": start_date.isoformat(),
        "toDate": today.isoformat(),
        "size": "50",
    }
    pages = []
    transactions = []
    page_number = 0
    while True:
        # Request parameters are page/size; response fields are pageNumber/pageSize.
        payload = api_get(path, {**query, "page": str(page_number)})
        if not isinstance(payload, dict) or not isinstance(payload.get("transactions"), list):
            raise RuntimeError("Unexpected transactions response; export stopped.")
        if int(payload.get("pageNumber", -1)) != page_number:
            raise RuntimeError(
                f"Requested page {page_number}, received {payload.get('pageNumber')}; "
                "refusing an incomplete export."
            )
        pages.append(payload)
        transactions.extend(payload["transactions"])
        next_page = payload.get("nextPage")
        page_count = int(payload.get("pageCount", 0))
        if next_page is None:
            if page_number + 1 < page_count:
                next_page = page_number + 1
            else:
                break
        next_page = int(next_page)
        if next_page != page_number + 1:
            raise RuntimeError("Unexpected nextPage; refusing skipped or repeated pages.")
        page_number = next_page
    # Keep original responses for auditing; importer reads the combined list.
    return {
        "transactions": transactions,
        "pages": pages,
        "fromDate": query["fromDate"],
        "toDate": query["toDate"],
        "pagesDownloaded": len(pages),
    }


def request_json(request: urllib.request.Request) -> dict | list:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error


class Handler(BaseHTTPRequestHandler):
    def is_local_request(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        is_local = self.is_local_request()
        # A tunnel makes the callback reachable by the bank, but all financial
        # pages remain available only through localhost on this computer.
        if not is_local and parsed.path != "/oauth/callback":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            if parsed.path == "/":
                self.page("Financials x01", home_content())
            elif parsed.path == "/login":
                self.redirect(authorization_url())
            elif parsed.path == "/oauth/callback":
                self.oauth_callback(urllib.parse.parse_qs(parsed.query))
            elif parsed.path == "/export-json":
                if not SESSION["access_token"]:
                    self.redirect("/login")
                else:
                    export_dir, file_count = export_raw_json()
                    self.page(
                        "JSON export complete",
                        "<h1>JSON export complete</h1>"
                        f"<p>Saved {file_count} JSON files locally, including all transaction pages.</p>"
                        f"<pre>{html.escape(str(export_dir))}</pre>",
                    )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            if is_local:
                details = f"<pre>{html.escape(str(error))}</pre>"
            else:
                details = "<p>Authorization could not be completed. Return to the local app.</p>"
            self.page("Connection error", f"<h1>Connection error</h1>{details}", 500)

    def oauth_callback(self, query: dict[str, list[str]]) -> None:
        if query.get("error"):
            raise RuntimeError(query["error"][0])
        state = query.get("state", [""])[0]
        if not state or not secrets.compare_digest(state, SESSION["state"] or ""):
            raise RuntimeError("OAuth state mismatch")
        code = query.get("code", [""])[0]
        if not code:
            raise RuntimeError("Authorization code missing")
        token_data = exchange_code(code)
        SESSION["access_token"] = token_data.get("access_token")
        SESSION["refresh_token"] = token_data.get("refresh_token")
        if not SESSION["access_token"]:
            raise RuntimeError(f"Token response did not contain access_token: {token_data}")
        # Leave the public callback address immediately and save raw API JSON locally.
        self.redirect("http://127.0.0.1:5000/export-json")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def page(self, title: str, body: str, status: int = 200) -> None:
        document = f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title>
        <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
        <style>
        :root{{--ink:#17202a;--muted:#697586;--line:#e6e9ee;--blue:#075aa8;--blue2:#0e77d2;--bg:#f5f7fa;--green:#087a55;--red:#b42318}}
        *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,Segoe UI,sans-serif}}
        header{{background:#073f68;color:white;padding:20px 0}} .wrap{{width:min(1160px,calc(100% - 32px));margin:auto}}
        nav{{display:flex;align-items:center;justify-content:space-between;gap:20px}} .brand{{font-size:21px;font-weight:750}}
        .sandbox{{font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;background:#fff1c7;color:#6a4a00;padding:7px 10px;border-radius:999px}}
        main{{padding:34px 0 60px}} h1{{font-size:31px;margin:0 0 7px}} h2{{font-size:19px;margin:0 0 18px}} p{{color:var(--muted)}}
        .button{{display:inline-block;padding:12px 18px;background:var(--blue);color:white;text-decoration:none;border-radius:8px;font-weight:650}}
        .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:16px;margin:25px 0}}
        .card,.panel{{background:white;border:1px solid var(--line);border-radius:14px;box-shadow:0 2px 8px #17202a0b}}
        .card{{padding:20px}} .eyebrow{{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}}
        .amount{{font-size:29px;font-weight:760;margin:7px 0}} .account-name{{font-weight:700;margin-bottom:4px}} .account-number{{color:var(--muted);font-size:13px}}
        .summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0 28px}} .metric{{padding:18px 20px}} .metric strong{{display:block;font-size:22px;margin-top:5px}}
        .positive{{color:var(--green)}} .negative{{color:var(--red)}} .panel{{padding:22px;overflow:hidden}}
        table{{width:100%;border-collapse:collapse}} th{{text-align:left;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}} th,td{{padding:13px 10px;border-bottom:1px solid var(--line)}} td:last-child,th:last-child{{text-align:right}} tr:last-child td{{border-bottom:0}}
        .empty,.warning{{padding:18px;border-radius:10px;background:#f8fafc;color:var(--muted)}} .warning{{background:#fff7e6;color:#714b00}}
        details{{margin-top:18px}} pre{{white-space:pre-wrap;overflow:auto;background:#111827;color:#e5e7eb;padding:18px;border-radius:8px}}
        @media(max-width:700px){{.summary{{grid-template-columns:1fr}} .hide-mobile{{display:none}} h1{{font-size:26px}}}}
        </style>
        </head><body>{body}</body></html>"""
        encoded = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        # Authorization codes and OAuth state are sensitive and must not appear
        # in terminal logs or screenshots.
        safe_args = tuple(
            re.sub(r"(/oauth/callback)\?[^ ]+", r"\1?[redacted]", value)
            if isinstance(value, str)
            else value
            for value in args
        )
        print(f"[web] {format % safe_args}")



## Creates a clickable link to a URL within your Python web application.
def home_content() -> str:
    environment = os.environ.get("ERSTE_ENVIRONMENT", "sandbox").strip().title()
    if SESSION["access_token"]:
        return f'<h1>Financials x01</h1><p>{environment} authorization is active.</p><a class="button" href="/export-json">Download raw JSON</a>'
    return f'<h1>Financials x01</h1><p>Authorize read-only access and save the original API responses as local JSON files.</p><a class="button" href="/login">Get bank JSON buraya yaziyoz</a>'



def write_json(path: Path, payload: dict | list) -> None:
    """Write the decoded API response without changing its structure or values."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_raw_json() -> tuple[Path, int]:
    """Fetch all responses before publishing a snapshot for the importer."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_dir = RAW_DATA_ROOT / timestamp
    accounts_payload = get_accounts()
    files = {"accounts.json": accounts_payload}

    accounts = accounts_payload.get("accounts", []) if isinstance(accounts_payload, dict) else accounts_payload
    for index, account in enumerate(accounts, start=1):
        if not isinstance(account, dict) or not account.get("id"):
            continue
        account_id = str(account["id"])
        prefix = f"account_{index:03d}"
        files[f"{prefix}_balance.json"] = get_balance(account_id)
        files[f"{prefix}_transactions.json"] = get_transactions(account_id)
    # A failed API call must not leave a newer, incomplete snapshot.
    export_dir.mkdir(parents=True, exist_ok=False)
    for filename, payload in files.items():
        write_json(export_dir / filename, payload)
    return export_dir, len(files)


if __name__ == "__main__":
    load_env()
    server = ThreadingHTTPServer(("127.0.0.1", 5000), Handler)
    print("Open http://127.0.0.1:5000")
    server.serve_forever()
