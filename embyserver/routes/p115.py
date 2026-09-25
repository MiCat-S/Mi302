"""115 掃碼登入的管理 API、網頁，以及 pickcode 302 端點。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import httpx

from ..auth import AuthContext, require_admin
from ..config import StrmTask
from ..p115 import PICKCODE_RE, P115Error
from ..p115_open import P115OpenError
from .common import q, state

router = APIRouter()


@router.get("/p115/status")
def p115_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    svc = st.p115
    # 管理員從哪個網址開這個頁面，播放器通常也連得到，拿來當 strm 裡的伺服器位址
    st.strm_sync.remember_base_url(str(request.base_url))
    return {
        "logged_in": svc.logged_in,
        "cookie": bool(svc.cookies),
        "user": svc.user_info() if svc.cookies else None,
        "open": svc.open.status(),
    }


@router.post("/p115/open/qrcode")
async def p115_open_qrcode(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    try:
        return state(request).p115.open.qrcode_start(str(body.get("app_id") or ""))
    except (P115OpenError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/p115/open/qrcode/status")
def p115_open_qrcode_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    uid, time_, sign = q(request, "uid"), q(request, "time"), q(request, "sign")
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 uid")
    try:
        return state(request).p115.open.qrcode_status(uid, time_ or "", sign or "")
    except (P115OpenError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/p115/open/logout")
def p115_open_logout(request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).p115.open.logout()
    return Response(status_code=204)


@router.post("/p115/qrcode")
def p115_qrcode(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        return state(request).p115.qrcode_token()
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/p115/qrcode/status")
def p115_qrcode_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    uid, time_, sign = q(request, "uid"), q(request, "time"), q(request, "sign")
    if not (uid and time_ and sign):
        raise HTTPException(status_code=400, detail="缺少 uid、time 或 sign")
    try:
        return state(request).p115.qrcode_status(uid, time_, sign)
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/p115/cookies")
async def p115_set_cookies(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    cookies = (body.get("cookies") or "").strip()
    if not cookies:
        raise HTTPException(status_code=400, detail="cookies 不可為空")
    svc = state(request).p115
    svc.set_cookies(cookies)
    return {"logged_in": True, "user": svc.user_info()}


@router.post("/p115/logout")
def p115_logout(request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).p115.logout()
    return Response(status_code=204)


def _tasks_view(st) -> list:
    libs = [Path(p).expanduser().resolve() for lib in st.config.libraries for p in lib.paths]

    def in_library(local: str) -> bool:
        path = Path(local).expanduser().resolve()
        return any(path == lib or lib in path.parents or path in lib.parents for lib in libs)

    return [
        {"remote": t.remote, "local": t.local, "in_library": in_library(t.local)}
        for t in st.strm_sync.tasks
    ]


@router.post("/p115/strm/sync")
def p115_strm_sync(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    if not st.p115.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    if not st.strm_sync.tasks:
        raise HTTPException(status_code=400, detail="還沒有同步任務，請先新增「115 目錄 → 本機資料夾」")
    st.strm_sync.remember_base_url(str(request.base_url))
    started = st.strm_sync.run_in_background()
    return {"started": started, "result": st.strm_sync.result.as_dict()}


@router.get("/p115/strm/status")
def p115_strm_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    return {
        "tasks": _tasks_view(st),
        "libraries": [{"name": lib.name, "type": lib.type, "paths": lib.paths} for lib in st.config.libraries],
        "base_url": st.strm_sync.base_url,
        "result": st.strm_sync.result.as_dict(),
    }


@router.put("/p115/strm/tasks")
async def p115_strm_tasks(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"[]")
    except ValueError:
        raise HTTPException(status_code=400, detail="格式錯誤")
    tasks = []
    for t in body if isinstance(body, list) else []:
        remote = str(t.get("remote") or "").strip()
        local = str(t.get("local") or "").strip()
        if not (remote and local):
            raise HTTPException(status_code=400, detail="115 目錄和本機資料夾都要填")
        if not remote.startswith("/"):
            remote = "/" + remote
        tasks.append(StrmTask(remote=remote, local=local))
    st = state(request)
    st.strm_sync.set_tasks(tasks)
    return {"tasks": _tasks_view(st)}


def _redirect(request: Request, pickcode: str) -> Response:
    if not PICKCODE_RE.match(pickcode):
        raise HTTPException(status_code=400, detail=f"Bad pickcode: {pickcode}")
    try:
        url = state(request).p115.download_url(pickcode, request.headers.get("user-agent", ""))
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RedirectResponse(url=url, status_code=302)


# 本伺服器產生的 strm：/d/{pickcode}.mkv（可再帶 ?/原檔名 或 /原檔名，會被忽略）
@router.api_route("/d/{code}", methods=["GET", "HEAD"])
@router.api_route("/d/{code}/{name:path}", methods=["GET", "HEAD"])
def p115_short_link(code: str, request: Request):
    return _redirect(request, code.split(".", 1)[0])


# 相容其他工具產生的 strm（例如 P115StrmHelper），換個主機即可沿用
@router.api_route("/p115/redirect", methods=["GET", "HEAD"])
@router.api_route("/api/v1/plugin/p115strmhelper/redirect_url", methods=["GET", "HEAD"])
def p115_redirect(request: Request):
    return _redirect(request, q(request, "pickcode") or q(request, "pick_code") or "")


@router.get("/web/115")
def p115_page():
    return HTMLResponse(LOGIN_PAGE)




LOGIN_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>115 設定</title>
<style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#222}
input,button,select{font-size:16px;padding:8px;margin:4px 0;width:100%;box-sizing:border-box}
button{cursor:pointer}#qr img,#oqr img{width:220px;height:220px;display:block;margin:12px auto}
.muted{color:#777;font-size:14px}.warn{color:#b35c00;font-size:14px}
.box{border:1px solid #ddd;border-radius:8px;padding:16px;margin:16px 0}
textarea{width:100%;box-sizing:border-box;height:80px}
summary{cursor:pointer;margin-top:8px}
.task{display:flex;gap:8px;align-items:center;border-top:1px solid #eee;padding:6px 0}
.task div{flex:1;word-break:break-all}.task button{width:auto}
</style></head><body>
<h2>115 網盤設定</h2>
<div id="login" class="box">
  <p class="muted">用設定檔裡的管理員帳號登入</p>
  <input id="u" placeholder="使用者名稱"><input id="p" type="password" placeholder="密碼">
  <button onclick="login()">登入</button>
</div>
<div id="main" style="display:none">
  <div class="box">
    <b>第 1 步：登入 115</b>
    <p>目前狀態：<span id="st">讀取中…</span></p>
    <button onclick="startQr()">產生登入二維碼</button>
    <p class="muted">用手機 115 App 掃描並確認。登入後同類型裝置（預設是支付寶小程式）的舊登入會被踢下線。</p>
    <div id="qr"></div><p id="qrst" class="muted"></p>
    <details><summary class="muted">改用貼上 cookie</summary>
      <p class="muted">格式像 UID=…; CID=…; SEID=…</p>
      <textarea id="ck"></textarea><button onclick="saveCookie()">儲存 cookie</button>
    </details>
    <details><summary class="muted">進階：115 開放平台（需要自己申請的 AppID，一般用戶不用管）</summary>
      <p class="muted">只有在 open.115.com 申請到應用的人才用得到。授權後取直鏈會優先走開放平台，失敗再用上面的登入。</p>
      <input id="appid" placeholder="AppID">
      <button onclick="startOpenQr()">產生開放平台授權二維碼</button>
      <div id="oqr"></div><p id="oqrst" class="muted"></p>
      <button onclick="logoutOpen()">取消開放平台授權</button>
    </details>
    <button onclick="logout115()">登出 115</button>
  </div>
  <div class="box">
    <b>第 2 步：選擇要產生 strm 的 115 目錄</b>
    <p class="muted">伺服器會把 115 目錄裡的影片產生成 .strm，放到本機資料夾，目錄結構不變。本機資料夾要在媒體庫路徑裡面，播放器才看得到。</p>
    <div id="tasks"></div>
    <input id="remote" placeholder="115 目錄，例如 /影視/電影">
    <select id="local"></select>
    <input id="localtxt" placeholder="或自行輸入本機資料夾" style="display:none">
    <button onclick="addTask()">新增</button>
    <p id="taskst" class="warn"></p>
  </div>
  <div class="box">
    <b>第 3 步：同步</b>
    <p class="muted">strm 內的伺服器位址：<span id="base"></span></p>
    <button onclick="syncStrm()">立即從 115 同步 strm</button>
    <p id="syncst" class="muted"></p>
  </div>
</div>
<script>
let token = sessionStorage.getItem('t') || '';
let tasks = [];
const H = () => ({'X-Emby-Token': token, 'Content-Type': 'application/json'});
async function api(path, opt = {}) {
  const r = await fetch(path, {...opt, headers: H()});
  if (!r.ok) throw new Error(await r.text() || r.status);
  return r.status === 204 ? null : r.json();
}
async function login() {
  const r = await fetch('/Users/AuthenticateByName', {method: 'POST',
    headers: {'Content-Type': 'application/json',
      'X-Emby-Authorization': 'MediaBrowser Client="Web115", Device="Browser", DeviceId="web115", Version="1"'},
    body: JSON.stringify({Username: u.value, Pw: p.value})});
  if (!r.ok) { alert('登入失敗'); return; }
  token = (await r.json()).AccessToken; sessionStorage.setItem('t', token); show();
}
async function show() {
  let s;
  try { s = await api('/p115/status'); } catch (e) { sessionStorage.removeItem('t'); token = ''; return; }
  document.getElementById('login').style.display = 'none';
  document.getElementById('main').style.display = '';
  let text = s.cookie ? ('已登入' + (s.user ? '（' + s.user.user_name + '）' : '（可能已失效，請重新掃碼）')) : '未登入';
  if (s.open.authorized) text += '，開放平台已授權';
  st.textContent = text;
  if (s.open.app_id && !appid.value) appid.placeholder = 'AppID：' + s.open.app_id;
  pollSync(true);
}
function pollQr(t, statusUrl, box, label, done) {
  const poll = async () => {
    try {
      const r = await api(statusUrl + '?uid=' + t.uid + '&time=' + t.time + '&sign=' + t.sign);
      const msg = {waiting: '等待掃描', scanned: '已掃描，請在手機上確認', success: '成功',
                   expired: '二維碼已過期，請重新產生', canceled: '已取消'}[r.status] || r.status;
      label.textContent = msg;
      if (r.status === 'success') { box.innerHTML = ''; show(); return; }
      if (r.status === 'expired' || r.status === 'canceled') return;
    } catch (e) { label.textContent = '錯誤：' + e.message; return; }
    done.timer = setTimeout(poll, 1500);
  };
  poll();
}
const qrTimer = {}, oqrTimer = {};
async function startQr() {
  clearTimeout(qrTimer.timer);
  let t;
  try { t = await api('/p115/qrcode', {method: 'POST'}); } catch (e) { qrst.textContent = '錯誤：' + e.message; return; }
  qr.innerHTML = '<img src="' + t.qrcode_image + '">';
  qrst.textContent = '請用 115 App 掃描';
  pollQr(t, '/p115/qrcode/status', qr, qrst, qrTimer);
}
async function startOpenQr() {
  clearTimeout(oqrTimer.timer);
  let t;
  try { t = await api('/p115/open/qrcode', {method: 'POST', body: JSON.stringify({app_id: appid.value})}); }
  catch (e) { oqrst.textContent = '錯誤：' + e.message; return; }
  oqr.innerHTML = '<img src="' + t.qrcode_image + '">';
  oqrst.textContent = '請用 115 App 掃描並授權';
  pollQr(t, '/p115/open/qrcode/status', oqr, oqrst, oqrTimer);
}
async function logoutOpen() { await api('/p115/open/logout', {method: 'POST'}); show(); }
async function logout115() { await api('/p115/logout', {method: 'POST'}); show(); }
async function saveCookie() {
  try { await api('/p115/cookies', {method: 'POST', body: JSON.stringify({cookies: ck.value})}); }
  catch (e) { alert(e.message); return; }
  ck.value = ''; show();
}
function renderTasks(list) {
  tasks = list.map(t => ({remote: t.remote, local: t.local}));
  const box = document.getElementById('tasks');
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<p class="muted">還沒有任務</p>'; }
  list.forEach((t, i) => {
    const row = document.createElement('div'); row.className = 'task';
    const txt = document.createElement('div');
    txt.textContent = '115:' + t.remote + ' → ' + t.local;
    if (!t.in_library) {
      const w = document.createElement('div'); w.className = 'warn';
      w.textContent = '這個資料夾不在任何媒體庫裡，播放器會看不到';
      txt.appendChild(w);
    }
    const del = document.createElement('button'); del.textContent = '刪除';
    del.onclick = () => saveTasks(tasks.filter((_, j) => j !== i));
    row.appendChild(txt); row.appendChild(del); box.appendChild(row);
  });
}
function renderLibraries(libs) {
  const sel = document.getElementById('local');
  if (sel.options.length) return;
  libs.forEach(l => l.paths.forEach(p => {
    const o = document.createElement('option'); o.value = p; o.textContent = '媒體庫「' + l.name + '」：' + p; sel.appendChild(o);
  }));
  const o = document.createElement('option'); o.value = ''; o.textContent = '其他資料夾…'; sel.appendChild(o);
  sel.onchange = () => { localtxt.style.display = sel.value ? 'none' : ''; };
  sel.onchange();
}
async function saveTasks(list) {
  taskst.textContent = '';
  try { renderTasks((await api('/p115/strm/tasks', {method: 'PUT', body: JSON.stringify(list)})).tasks); }
  catch (e) { taskst.textContent = '錯誤：' + e.message; }
}
function addTask() {
  const loc = local.value || localtxt.value.trim();
  if (!remote.value.trim() || !loc) { taskst.textContent = '115 目錄和本機資料夾都要填'; return; }
  saveTasks(tasks.concat([{remote: remote.value.trim(), local: loc}]));
  remote.value = ''; localtxt.value = '';
}
async function syncStrm() {
  try { await api('/p115/strm/sync', {method: 'POST'}); } catch (e) { syncst.textContent = '錯誤：' + e.message; return; }
  pollSync(false);
}
async function pollSync(first) {
  const s = await api('/p115/strm/status');
  if (first) { renderTasks(s.tasks); renderLibraries(s.libraries); }
  base.textContent = s.base_url;
  const r = s.result;
  if (!r.started) { syncst.textContent = ''; return; }
  syncst.textContent = (r.running ? '同步中… ' : '上次同步完成：') + '新增/更新 ' + r.strm_created + '，未變 ' + r.strm_unchanged +
    '，下載中繼資料 ' + r.metadata_downloaded + '，刪除 ' + r.removed + (r.errors.length ? '，錯誤 ' + r.errors.length + '：' + r.errors.slice(0, 3).join('；') : '');
  if (r.running) setTimeout(() => pollSync(false), 2000);
}
if (token) show();
</script></body></html>
"""
