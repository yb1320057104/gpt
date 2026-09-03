"""Opt-in end-to-end mail.com mailbox provisioning for GPT-Register-Tool."""
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

def _json(url, *, method="GET", body=None, token="", timeout=30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}

def _text(url, *, token="", timeout=30):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")

def ensure_service(root: Path, *, port=8790):
    base = f"http://127.0.0.1:{int(port)}"
    try: _json(base + "/health", timeout=2); return None, base
    except Exception: pass
    service = root / "services" / "mail-com-code-api"; data = service / "data"; data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env.update({"MAIL_API_BIND":"127.0.0.1", "MAIL_API_PORT":str(port), "MAIL_API_PUBLIC_BASE":base})
    deps = root.parent / ".runtime_deps"
    if deps.is_dir(): env["PYTHONPATH"] = str(deps) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen([sys.executable,"server.py","--bind","127.0.0.1","--port",str(port),"--public-base",base,"--data-dir",str(data)], cwd=service, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try: _json(base + "/health", timeout=2); return proc, base
        except Exception: time.sleep(.25)
    raise RuntimeError("mail.com API 启动失败")

def sync_mailboxes(root, credentials_file, output_file=None, *, port=8790, verify=True, sync_aliases=True):
    root = Path(root).resolve(); credentials = Path(credentials_file).expanduser().resolve()
    lines = [x.strip() for x in credentials.read_text(encoding="utf-8-sig").splitlines() if x.strip() and not x.lstrip().startswith("#")]
    if not lines: raise ValueError("mail.com 凭据文件为空")
    proc, base = ensure_service(root, port=port)
    token_path = root / "services" / "mail-com-code-api" / "data" / "admin.token"
    for _ in range(20):
        if token_path.is_file(): break
        time.sleep(.25)
    token = token_path.read_text(encoding="ascii").strip() if token_path.is_file() else ""
    result = _json(base + "/admin/import?verify=" + ("true" if verify else "false") + "&sync_aliases=" + ("true" if sync_aliases else "false"), method="POST", body={"lines":lines}, token=token, timeout=120)
    text = _text(base + "/admin/export", token=token, timeout=30)
    target = Path(output_file).expanduser().resolve() if output_file else root / "mailcom_mailbox_pool.txt"
    target.write_text(str(text), encoding="utf-8")
    return {"ok":True, "import":result, "output_file":str(target), "lines":len([x for x in str(text).splitlines() if x.strip()]), "service":base}
