import os
import subprocess
import sys
import tempfile
import ast
import base64
import json
import urllib.parse
import hashlib
import zlib
import re
import secrets
import sqlite3

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Python Playground API")

DEFAULT_ALLOWED_ORIGINS = {
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://pravin22kumar.github.io",
}


def allowed_origins():
    configured = os.getenv("ALLOWED_ORIGINS", "")
    return sorted(
        DEFAULT_ALLOWED_ORIGINS
        | {
            origin.strip().rstrip("/")
            for origin in configured.split(",")
            if origin.strip()
        }
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.get("/api/files")
def list_repository_files(search: str = "", x_github_token: str | None = Header(default=None)):
    import json
    import urllib.error
    import urllib.request

    owner = os.getenv("GITHUB_OWNER", "pravin22kumar")
    repo = os.getenv("GITHUB_REPO", "Python")
    token = x_github_token or os.getenv("GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "python-playground",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            items = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 401 and token:
            # Repository browsing can remain available for a public repository
            # even when a deployment has an expired GitHub token. PR creation
            # still requires a valid token in /api/save.
            anonymous_headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "python-playground",
            }
            try:
                anonymous_request = urllib.request.Request(url, headers=anonymous_headers)
                with urllib.request.urlopen(anonymous_request, timeout=10) as response:
                    items = json.loads(response.read())
            except urllib.error.HTTPError:
                raise HTTPException(401, "GitHub token is invalid and the repository is not publicly readable")
            except urllib.error.URLError as anonymous_error:
                raise HTTPException(503, f"Repository file service is unavailable: {anonymous_error.reason}")
        else:
            try:
                detail = json.loads(error.read().decode("utf-8", "replace"))
                message = detail.get("message", "Could not load repository files")
            except (ValueError, UnicodeDecodeError):
                message = "Could not load repository files"
            raise HTTPException(error.code, f"GitHub repository files: {message}")
    except urllib.error.URLError as error:
        raise HTTPException(503, f"Repository file service is unavailable: {error.reason}")

    query = search.strip().lower()
    return [
        {
            "name": item["name"],
            "type": item["type"],
            "download_url": item.get("download_url"),
        }
        for item in items
        if item.get("type") == "file"
        and item.get("name", "").endswith(".py")
        and item.get("name", "").lower() != "app.py"
        and (not query or query in item.get("name", "").lower())
    ]


@app.get("/api/files/check")
def check_file_name(name: str, x_github_token: str | None = Header(default=None)):
    normalized = name.strip().lower()
    files = list_repository_files(x_github_token=x_github_token)
    return {
        "exists": any(item["name"].lower() == normalized for item in files),
        "allowed": normalized.endswith(".py") and normalized != "app.py",
    }


class RunRequest(BaseModel):
    code: str
    stdin: str = ""
    timeout: int = Field(default=5, ge=1, le=30)
    memory_mb: int = Field(default=128, ge=32, le=512)
    network_disabled: bool = True
    test_framework: str = "none"
    dependencies: list[str] = Field(default_factory=list)


class SaveRequest(BaseModel):
    path: str
    content: str
    title: str = "Update Python file"
    base: str = "main"
    contributor_id: str = Field(pattern=r"^#[a-z0-9]+@[0-9]{5}$")
    changes: list[dict] = Field(default_factory=list)

class ShareRequest(BaseModel):
    name: str
    code: str


def share_cipher():
    secret = os.getenv("SHARE_SECRET", "local-python-playground-share-secret")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def valid_share_name(name: str):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+\.py", name or "")) and name.lower() != "app.py"


def share_db():
    path = os.getenv("SHARE_DB", "shares.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
    connection.commit()
    return connection


ADMIN_CONTRIBUTOR_ID = "#pravin@89190"


def users_db():
    path = os.getenv("SHARE_DB", "shares.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
    connection.execute("INSERT OR IGNORE INTO users (id, name, phone_hash) VALUES (?, ?, ?)", (ADMIN_CONTRIBUTOR_ID, "Pravin Kumar", hashlib.sha256(b"9150089190").hexdigest()))
    connection.commit()
    return connection


class UserRequest(BaseModel):
    id: str
    name: str = ""
    phone: str = ""


class ChangePreviewRequest(BaseModel):
    changes: list[dict] = Field(default_factory=list)


@app.post("/api/source-control/preview")
def source_control_preview(request: ChangePreviewRequest):
    allowed = {"added": "A", "modified": "M", "renamed": "R", "deleted": "D"}
    preview = []
    for change in request.changes:
        change_type = change.get("type", "modified")
        if change_type not in allowed:
            raise HTTPException(400, f"Unsupported change type: {change_type}")
        path = change.get("path", "")
        if not isinstance(path, str) or not path or ".." in path.split("/"):
            raise HTTPException(400, "Invalid change path")
        preview.append({"status": allowed[change_type], "type": change_type, "path": path})
    return {"count": len(preview), "changes": preview}


def valid_contributor_id(value: str):
    return bool(re.fullmatch(r"#[a-z0-9]+@[0-9]{5}", value or ""))


@app.post("/api/users/register")
def register_user(request: UserRequest):
    user_id = request.id.strip().lower()
    if not valid_contributor_id(user_id):
        raise HTTPException(400, "Contributor ID must look like #name@12345")
    phone_digits = re.sub(r"\D", "", request.phone)
    if not request.name.strip() or len(phone_digits) < 5:
        raise HTTPException(400, "Name and a valid phone number are required")
    connection = users_db()
    try:
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(409, "Contributor ID is already registered")
        phone_hash = hashlib.sha256(phone_digits.encode()).hexdigest()
        if connection.execute("SELECT 1 FROM users WHERE phone_hash = ?", (phone_hash,)).fetchone():
            raise HTTPException(409, "This phone number is already registered")
        connection.execute("INSERT INTO users (id, name, phone_hash) VALUES (?, ?, ?)", (user_id, request.name.strip(), phone_hash))
        connection.commit()
    finally:
        connection.close()
    return {"id": user_id}


@app.get("/api/users/check/{user_id}")
def check_user(user_id: str):
    connection = users_db()
    try:
        row = connection.execute("SELECT active FROM users WHERE id = ?", (user_id.lower(),)).fetchone()
    finally:
        connection.close()
    return {"available": row is None, "active": bool(row and row[0])}


@app.get("/api/admin/users")
def list_users(x_admin_id: str = ""):
    if x_admin_id.lower() != ADMIN_CONTRIBUTOR_ID:
        raise HTTPException(403, "Admin access required")
    connection = users_db()
    try:
        rows = connection.execute("SELECT id, name, active FROM users ORDER BY name").fetchall()
    finally:
        connection.close()
    return [{"id": row[0], "name": row[1], "active": bool(row[2])} for row in rows]


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, x_admin_id: str = ""):
    if x_admin_id.lower() != ADMIN_CONTRIBUTOR_ID:
        raise HTTPException(403, "Admin access required")
    if user_id.lower() == ADMIN_CONTRIBUTOR_ID:
        raise HTTPException(400, "The admin account cannot be deleted")
    connection = users_db()
    try:
        connection.execute("DELETE FROM users WHERE id = ?", (user_id.lower(),))
        connection.commit()
    finally:
        connection.close()
    return {"deleted": user_id.lower()}


@app.put("/api/admin/users/{user_id}")
def edit_user(user_id: str, request: UserRequest, x_admin_id: str = ""):
    if x_admin_id.lower() != ADMIN_CONTRIBUTOR_ID:
        raise HTTPException(403, "Admin access required")
    new_id = request.id.strip().lower()
    if not valid_contributor_id(new_id) or not request.name.strip():
        raise HTTPException(400, "Valid ID and name are required")
    connection = users_db()
    try:
        connection.execute("UPDATE users SET id = ?, name = ? WHERE id = ?", (new_id, request.name.strip(), user_id.lower()))
        connection.commit()
    finally:
        connection.close()
    return {"id": new_id, "name": request.name.strip()}


@app.post("/api/share")
def create_share(request: ShareRequest):
    if not valid_share_name(request.name):
        raise HTTPException(400, "Only Python filenames are allowed")
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")
    payload = json.dumps({"name": request.name, "code": request.code}, separators=(",", ":")).encode()
    encrypted = share_cipher().encrypt(zlib.compress(payload, level=9))
    connection = share_db()
    try:
        share_id = secrets.token_urlsafe(8)
        connection.execute("INSERT INTO shares (id, payload) VALUES (?, ?)", (share_id, encrypted))
        connection.commit()
    finally:
        connection.close()
    return {"token": share_id}


@app.get("/api/share/{token}")
def read_share(token: str):
    try:
        connection = share_db()
        row = connection.execute("SELECT payload FROM shares WHERE id = ?", (token,)).fetchone()
        connection.close()
        if not row:
            raise InvalidToken
        payload = zlib.decompress(share_cipher().decrypt(row[0]))
        result = json.loads(payload)
    except (InvalidToken, ValueError, zlib.error, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(404, "Share link is invalid or expired")
    if not valid_share_name(result.get("name", "")) or not isinstance(result.get("code"), str):
        raise HTTPException(404, "Share link is invalid")
    return result
    changes: list[dict] = Field(default_factory=list)


@app.get("/")
def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/run")
def run_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")

    if len(request.stdin) > 10_000:
        raise HTTPException(400, "Standard input exceeds the 10 KB limit")
    if request.test_framework not in {"none", "unittest", "pytest"}:
        raise HTTPException(400, "Unsupported test framework")

    allowed_dependencies = {
        item.strip().lower()
        for item in os.getenv("ALLOWED_PACKAGES", "").split(",")
        if item.strip()
    }
    unknown_dependencies = sorted({
        dependency.lower()
        for dependency in request.dependencies
        if dependency.lower() not in allowed_dependencies
    })
    if unknown_dependencies:
        raise HTTPException(
            400,
            "Dependencies are not allowlisted: " + ", ".join(unknown_dependencies),
        )

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as source:
        bootstrap = ""
        if request.network_disabled:
            bootstrap = (
                "import sys\n"
                "def _block_network(event, args):\n"
                "    if event.startswith('socket.'):\n"
                "        raise PermissionError('Network access is disabled for this run')\n"
                "sys.addaudithook(_block_network)\n"
            )
        bootstrap += (
            "import resource\n"
            f"_limit = {request.memory_mb} * 1024 * 1024\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_AS, (_limit, _limit))\n"
            "except (AttributeError, ValueError, OSError):\n"
            "    pass\n"
        )
        source.write(bootstrap)
        source.write(request.code)
        if request.test_framework == "unittest":
            source.write(
                "\n\nimport unittest\n"
                "_suite = unittest.defaultTestLoader.loadTestsFromModule("
                "sys.modules[__name__])\n"
                "_result = unittest.TextTestRunner().run(_suite)\n"
                "raise SystemExit(0 if _result.wasSuccessful() else 1)\n"
            )
        source_path = source.name

    try:
        argv = [sys.executable, "-I", "-B", source_path]
        if request.test_framework == "pytest":
            argv = [sys.executable, "-I", "-B", "-m", "pytest", "-q", source_path]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            input=request.stdin,
            timeout=request.timeout,
            cwd=tempfile.gettempdir(),
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Execution timed out after {request.timeout} seconds.",
            "exit_code": 124,
        }
    finally:
        os.unlink(source_path)


@app.post("/api/lint")
def lint_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")
    try:
        ast.parse(request.code)
    except SyntaxError as error:
        return {
            "valid": False,
            "errors": [{
                "line": error.lineno or 1,
                "column": (error.offset or 1),
                "message": error.msg,
            }],
        }
    return {"valid": True, "errors": []}


@app.post("/api/format")
def format_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")
    try:
        tree = ast.parse(request.code)
    except SyntaxError as error:
        raise HTTPException(
            400,
            f"Syntax error on line {error.lineno or 1}: {error.msg}",
        )
    return {"code": ast.unparse(tree)}


@app.post("/api/save")
def save_as_pull_request(request: SaveRequest, x_github_token: str | None = Header(default=None)):
    token = x_github_token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(503, "GITHUB_TOKEN is not configured")

    import base64
    import json
    import secrets
    import urllib.error
    import urllib.parse
    import urllib.request

    owner = os.getenv("GITHUB_OWNER", "pravin22kumar")
    repo = os.getenv("GITHUB_REPO", "Python")
    def safe_path(value):
        path = value.replace("\\", "/").lstrip("/")
        if not path.endswith(".py") or ".." in path.split("/"):
            raise HTTPException(400, "Only .py files below the repository root are accepted")
        return path

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "python-playground",
    }

    def github_json(method, url, payload=None):
        data = json_bytes(payload)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise HTTPException(error.code, f"GitHub API error: {detail}")
        except urllib.error.URLError as error:
            raise HTTPException(502, f"GitHub API is unavailable: {error.reason}")
        except (TimeoutError, OSError) as error:
            raise HTTPException(502, f"GitHub connection failed: {error}")

    def github_optional_json(method, url, payload=None):
        data = json_bytes(payload)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            detail = error.read().decode("utf-8", "replace")
            raise HTTPException(error.code, f"GitHub API error: {detail}")
        except urllib.error.URLError as error:
            raise HTTPException(502, f"GitHub API is unavailable: {error.reason}")

    def json_bytes(payload):
        import json
        return json.dumps(payload).encode() if payload is not None else None

    base_url = f"https://api.github.com/repos/{owner}/{repo}"
    base_branch = github_json("GET", f"{base_url}/branches/{urllib.parse.quote(request.base, safe='')}")
    if not base_branch.get("commit", {}).get("sha"):
        raise HTTPException(502, "GitHub returned no commit SHA for the selected base branch")
    branch = f"python-playground/{secrets.token_hex(6)}"
    github_json(
        "POST",
        f"{base_url}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_branch["commit"]["sha"]},
    )

    changes = request.changes or [{"type": "modified", "path": request.path, "content": request.content}]
    summary = []
    for change in changes:
        change_type = change.get("type", "modified")
        path = safe_path(change.get("path", ""))
        previous_path = safe_path(change["previous_path"]) if change.get("previous_path") else None
        target_path = previous_path if change_type == "renamed" else path
        existing = github_optional_json(
            "GET",
            f"{base_url}/contents/{urllib.parse.quote(target_path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}",
        )
        sha = existing.get("sha")
        if change_type == "deleted" or change_type == "renamed":
            if sha:
                github_json(
                    "DELETE",
                    f"{base_url}/contents/{urllib.parse.quote(target_path, safe='/')}",
                    {"message": f"Delete {target_path}", "sha": sha, "branch": branch},
                )
            if change_type == "deleted":
                summary.append(f"D {target_path}")
                continue
        content = change.get("content", "")
        encoded = base64.b64encode(content.encode()).decode()
        github_json(
            "PUT",
            f"{base_url}/contents/{urllib.parse.quote(path, safe='/')}",
            {
                "message": f"Update {path}",
                "content": encoded,
                "branch": branch,
                **({"sha": sha} if change_type != "renamed" and sha else {}),
            },
        )
        summary.append(f"{change_type[0].upper()} {path}")

    pull_request = github_json(
        "POST",
        f"{base_url}/pulls",
        {
            "title": f"{request.contributor_id} {request.title}",
            "head": branch,
            "base": request.base,
            "body": f"Contributor: {request.contributor_id}\n\nUpdated by the Python Playground UI.\n\nChanges:\n" + "\n".join(f"- {item}" for item in summary),
        },
    )
    return {"pull_request_url": pull_request["html_url"], "branch": branch}
