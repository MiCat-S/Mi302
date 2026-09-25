"""115 掃碼登入的管理 API、網頁，以及 pickcode 302 端點。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import AuthContext, require_admin
from ..p115 import PICKCODE_RE, P115Error
from .common import q, state

router = APIRouter()


@router.get("/p115/status")
def p115_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    svc = state(request).p115
    return {"logged_in": svc.logged_in, "user": svc.user_info() if svc.logged_in else None}


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


@router.post("/p115/strm/sync")
def p115_strm_sync(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    if not st.p115.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    if not st.config.p115.strm.tasks:
        raise HTTPException(status_code=400, detail="設定檔裡沒有 p115.strm.tasks")
    started = st.strm_sync.run_in_background()
    return {"started": started, "result": st.strm_sync.result.as_dict()}


@router.get("/p115/strm/status")
def p115_strm_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    return {
        "tasks": [{"remote": t.remote, "local": t.local} for t in st.config.p115.strm.tasks],
        "result": st.strm_sync.result.as_dict(),
    }


# strm 可以直接寫成本伺服器的網址；也相容 P115StrmHelper 的路徑，舊 strm 換個主機即可沿用
@router.api_route("/p115/redirect", methods=["GET", "HEAD"])
@router.api_route("/api/v1/plugin/p115strmhelper/redirect_url", methods=["GET", "HEAD"])
def p115_redirect(request: Request):
    pickcode = q(request, "pickcode") or q(request, "pick_code") or ""
    if not PICKCODE_RE.match(pickcode):
        raise HTTPException(status_code=400, detail=f"Bad pickcode: {pickcode}")
    try:
        url = state(request).p115.download_url(pickcode, request.headers.get("user-agent", ""))
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RedirectResponse(url=url, status_code=302)


@router.get("/web/115")
def p115_page():
    return HTMLResponse(LOGIN_PAGE)


LOGIN_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>115 登入</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;color:#222}
input,button{font-size:16px;padding:8px;margin:4px 0;width:100%;box-sizing:border-box}
button{cursor:pointer}#qr img{width:220px;height:220px;display:block;margin:12px auto}
.muted{color:#777;font-size:14px}.box{border:1px solid #ddd;border-radius:8px;padding:16px;margin:16px 0}
textarea{width:100%;box-sizing:border-box;height:80px}
</style></head><body>
<h2>115 網盤登入</h2>
<div id="login" class="box">
  <p class="muted">先用本伺服器的管理員帳號登入</p>
  <input id="u" placeholder="使用者名稱"><input id="p" type="password" placeholder="密碼">
  <button onclick="login()">登入</button>
</div>
<div id="main" style="display:none">
  <div class="box"><b>目前狀態：</b><span id="st">讀取中…</span></div>
  <div class="box">
    <button onclick="startQr()">產生 115 登入二維碼</button>
    <div id="qr"></div><p id="qrst" class="muted"></p>
  </div>
  <div class="box">
    <p class="muted">或直接貼上 115 cookie（UID=…; CID=…; SEID=…）</p>
    <textarea id="ck"></textarea><button onclick="saveCookie()">儲存 cookie</button>
  </div>
  <div class="box">
    <b>產生 strm</b><div id="tasks" class="muted"></div>
    <button onclick="syncStrm()">立即從 115 同步 strm</button>
    <p id="syncst" class="muted"></p>
  </div>
  <button onclick="logout115()">登出 115</button>
</div>
<script>
let token = sessionStorage.getItem('t') || '';
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
  try { const s = await api('/p115/status');
    document.getElementById('login').style.display = 'none';
    document.getElementById('main').style.display = '';
    pollSync();
    st.textContent = s.logged_in ? ('已登入' + (s.user ? '：' + s.user.user_name : '（cookie 可能已失效）')) : '未登入';
  } catch (e) { sessionStorage.removeItem('t'); token = ''; }
}
let timer = null;
async function startQr() {
  clearTimeout(timer);
  const t = await api('/p115/qrcode', {method: 'POST'});
  qr.innerHTML = '<img src="' + t.qrcode_image + '">';
  qrst.textContent = '請用 115 App 掃描';
  const poll = async () => {
    try {
      const r = await api('/p115/qrcode/status?uid=' + t.uid + '&time=' + t.time + '&sign=' + t.sign);
      const msg = {waiting: '等待掃描', scanned: '已掃描，請在手機上確認', success: '登入成功',
                   expired: '二維碼已過期，請重新產生', canceled: '已取消'}[r.status] || r.status;
      qrst.textContent = msg;
      if (r.status === 'success') { qr.innerHTML = ''; show(); return; }
      if (r.status === 'expired' || r.status === 'canceled') return;
    } catch (e) { qrst.textContent = '錯誤：' + e.message; return; }
    timer = setTimeout(poll, 1500);
  };
  poll();
}
async function saveCookie() { await api('/p115/cookies', {method: 'POST', body: JSON.stringify({cookies: ck.value})}); ck.value = ''; show(); }
async function syncStrm() {
  try { await api('/p115/strm/sync', {method: 'POST'}); } catch (e) { syncst.textContent = '錯誤：' + e.message; return; }
  pollSync();
}
async function pollSync() {
  const s = await api('/p115/strm/status');
  tasks.innerHTML = s.tasks.map(t => '115:' + t.remote + ' → ' + t.local).join('<br>') || '設定檔裡還沒有同步任務';
  const r = s.result;
  if (!r.started) { syncst.textContent = ''; return; }
  syncst.textContent = (r.running ? '同步中… ' : '上次同步完成：') + '新增/更新 ' + r.strm_created + '，未變 ' + r.strm_unchanged +
    '，下載中繼資料 ' + r.metadata_downloaded + '，刪除 ' + r.removed + (r.errors.length ? '，錯誤 ' + r.errors.length + '：' + r.errors.slice(0, 3).join('；') : '');
  if (r.running) setTimeout(pollSync, 2000);
}
async function logout115() { await api('/p115/logout', {method: 'POST'}); show(); }
if (token) show();
</script></body></html>
"""
