#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到梦空间 (5idream.net) HTTP 客户端封装。

支持：
- 扫码登录二维码生成 (PNG + Base64 + 终端字符画) 与轮询登录态持久化
- 个人状态与学校信息识别
- 可报名活动查询（全国公开活动大厅 + 校内关注/报名中活动）
- 我的活动查询（已报名 / 已签到 / 我关注的 / 我管理的 / 我发起的）及并发详情富化
- 单个活动全方位详情（参加时间、报名时间、地点、内容、报名人数上限、已报名人数、积分/学分、奖项、组织者、附件）
- 个人学分/积分统计（按学分类型统计 + 按活动类型统计 + 学校全模块对照 + 活动明细）
- 我的部落、学校活动分类体系、官方公告与资讯查询
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import qrcode
import requests

BASE_URL = "https://www.5idream.net"
TZ_CN = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "session.json")
PENDING_LOGIN_FILE = os.path.join(HERE, "pending_login.json")
QR_IMAGE_FILE = os.path.join(HERE, "login_qrcode.png")

STATUS_MAP = {
    "1": "审核流程中",
    "2": "规划中",
    "3": "报名中",
    "4": "等待中(待开始)",
    "5": "进行中",
    "6": "已结束",
    "7": "已取消",
}

REVIEW_STATUS_MAP = {
    "1": "审核中",
    "2": "审核成功",
    "3": "审核驳回",
    "5": "待部落审核",
    "6": "待院级审核",
    "7": "待校级审核",
}

LEVEL_MAP = {
    "0": "院系级",
    "1": "校级",
    "2": "市级",
    "3": "省级",
    "4": "国家级",
}

JOIN_WAY_MAP = {
    "1": "中签制",
    "2": "报名制(先到先得)",
    "3": "评审制",
}

JOIN_METHOD_MAP = {
    "1": "个人报名",
    "2": "团队报名",
}

JOIN_RANGE_MAP = {
    "1": "部落内",
    "2": "学院内",
    "3": "学校内",
    "4": "不限范围",
}

PUBLIC_ACTIVITY_TYPE_MAP = {
    "1": "全国活动",
    "2": "全国赛事",
    "3": "培训",
    "4": "实践",
    "5": "实习",
}


class NotLoggedInError(RuntimeError):
    """未登录或登录态已失效。"""


def fmt_ts(ts: Any) -> str:
    """将毫秒时间戳格式化为北京时间 YYYY-MM-DD HH:MM。"""
    if ts is None or ts == "":
        return "暂无"
    s = str(ts).strip()
    if not s.isdigit():
        return s
    try:
        val = int(s)
        if val > 10_000_000_000:
            dt = datetime.fromtimestamp(val / 1000.0, tz=TZ_CN)
        else:
            dt = datetime.fromtimestamp(val, tz=TZ_CN)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def clean_html_text(raw: Any) -> str:
    """将富文本或带标签的内容清洗为可读纯文本。"""
    if not raw:
        return ""
    txt = str(raw)
    txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</p\s*>", "\n", txt)
    txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = txt.replace("\xa0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def format_status(status: Any, review_status: Any = None) -> str:
    st = str(status or "")
    if st == "1" and review_status is not None:
        return REVIEW_STATUS_MAP.get(str(review_status), "审核流程中")
    return STATUS_MAP.get(st, f"未知状态({st})" if st else "未知")


def render_qr_ascii(matrix: List[List[bool]]) -> str:
    """使用半高 Unicode 方块渲染紧凑的终端二维码（反色适配深浅终端）。"""
    rows = len(matrix)
    if rows == 0:
        return ""
    cols = len(matrix[0])
    out_lines: List[str] = []
    for r in range(0, rows, 2):
        line_chars: List[str] = []
        for c in range(cols):
            top = matrix[r][c]
            bot = matrix[r + 1][c] if (r + 1) < rows else False
            # 使用白底黑块视觉：空白为白色背景/全块，深色模块用实心或空心映射
            # 这里采用标准暗模块实心块：在浅色背景直接扫，深色背景加静区反转也可扫
            if top and bot:
                line_chars.append("█")
            elif top and not bot:
                line_chars.append("▀")
            elif not top and bot:
                line_chars.append("▄")
            else:
                line_chars.append(" ")
        out_lines.append("".join(line_chars))
    return "\n".join(out_lines)


class DreamSpaceClient:
    """到梦空间客户端，支持持久化 Cookie/Token 会话与并发请求。"""

    def __init__(self, session_file: str = SESSION_FILE):
        self.session_file = session_file
        self._session_mtime: float = 0.0
        self.token: str = ""
        self.userinfo: Dict[str, Any] = {}
        self.cookies_dict: Dict[str, str] = {}
        self.http = self._make_http_session()
        self._load_session_from_disk(force=True)

    def _make_http_session(self) -> requests.Session:
        s = requests.Session()
        s.trust_env = False  # 避免本地代理干扰国内站点访问
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/activity/myactivity.html",
            "X-Requested-With": "XMLHttpRequest",
        })
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def _load_session_from_disk(self, force: bool = False) -> None:
        if not os.path.exists(self.session_file):
            if force:
                self.token = ""
                self.userinfo = {}
                self.cookies_dict = {}
            return
        try:
            mtime = os.path.getmtime(self.session_file)
            if not force and mtime <= self._session_mtime:
                return
            with open(self.session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.token = data.get("token", "")
            self.userinfo = data.get("userinfo", {}) or {}
            self.cookies_dict = data.get("cookies", {}) or {}
            self.http.cookies.clear()
            for k, v in self.cookies_dict.items():
                self.http.cookies.set(k, v, domain="www.5idream.net", path="/")
            if self.token:
                self.http.cookies.set("dmkj_web_token", self.token, domain="www.5idream.net", path="/")
            self._session_mtime = mtime
        except Exception:
            pass

    def _save_session_to_disk(self) -> None:
        cookies_out: Dict[str, str] = {}
        for c in self.http.cookies:
            cookies_out[c.name] = c.value
        if self.token:
            cookies_out["dmkj_web_token"] = self.token
        self.cookies_dict = cookies_out
        payload = {
            "token": self.token,
            "userinfo": self.userinfo,
            "cookies": cookies_out,
            "updated_at": datetime.now(tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self._session_mtime = os.path.getmtime(self.session_file)

    def clear_session(self) -> None:
        if self.token:
            try:
                self._request("GET", "/token/loginout", params={"token": self.token}, check_redirect=False)
            except Exception:
                pass
        self.token = ""
        self.userinfo = {}
        self.cookies_dict = {}
        self.http.cookies.clear()
        if os.path.exists(self.session_file):
            try:
                os.remove(self.session_file)
            except OSError:
                pass

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        tries: int = 3,
        timeout: int = 20,
        check_redirect: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        self._load_session_from_disk()
        url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}{path_or_url}"
        last_err: Optional[Exception] = None
        for attempt in range(tries):
            try:
                resp = self.http.request(method, url, timeout=timeout, **kwargs)
                if check_redirect and resp.headers.get("REDIRECT") == "REDIRECT":
                    raise NotLoggedInError(
                        "当前到梦空间登录态已失效或未登录，请调用 dm_login_qrcode 获取二维码扫码登录。"
                    )
                return resp
            except NotLoggedInError:
                raise
            except Exception as e:
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"请求到梦空间失败 ({url}): {last_err}")

    # ------------------------------------------------------------------
    # 1. 扫码登录与会话管理
    # ------------------------------------------------------------------
    def create_login_qrcode(self, output_png: str = QR_IMAGE_FILE) -> Dict[str, Any]:
        """请求 getSecurityId 并生成官方格式二维码图片与字符矩阵。"""
        fresh = self._make_http_session()
        resp = fresh.get(f"{BASE_URL}/scan_login/getSecurityId", timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "100" or not body.get("result"):
            raise RuntimeError(f"获取扫码登录凭证失败: {body}")

        res = body["result"]
        sec_id = res["securityId"]
        scan_url = res["url"]
        qr_type = str(res.get("qrType", "4"))

        # 与 www.5idream.net/static/js/pages/login.js 严格一致：
        # var i = { qrCode: url + "?securityId=" + securityId, qrType: qrType };
        # text = JSON.stringify(i);
        qr_payload_obj = {
            "qrCode": f"{scan_url}?securityId={sec_id}",
            "qrType": qr_type,
        }
        qr_text = json.dumps(qr_payload_obj, separators=(",", ":"), ensure_ascii=False)

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_text)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        actual_png = output_png
        try:
            with open(actual_png, "wb") as f:
                f.write(png_bytes)
        except OSError:
            actual_png = os.path.join(HERE, f"login_qrcode_{sec_id[:8]}.png")
            with open(actual_png, "wb") as f:
                f.write(png_bytes)

        pending = {
            "security_id": sec_id,
            "qr_type": qr_type,
            "qr_payload": qr_text,
            "cookies": {c.name: c.value for c in fresh.cookies},
            "created_at": time.time(),
            "created_at_str": datetime.now(tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(PENDING_LOGIN_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)

        return {
            "security_id": sec_id,
            "qr_type": qr_type,
            "qr_payload": qr_text,
            "png_path": actual_png,
            "png_base64": base64.b64encode(png_bytes).decode("ascii"),
            "ascii_qr": render_qr_ascii(qr.get_matrix()),
            "created_at": pending["created_at_str"],
        }

    @staticmethod
    def _parse_jsonp(text: str) -> Dict[str, Any]:
        s = text.strip()
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError(f"无法解析响应内容: {s[:200]}")
        return json.loads(m.group(0))

    def poll_login(
        self,
        security_id: Optional[str] = None,
        wait_seconds: int = 60,
        interval: float = 2.0,
    ) -> Dict[str, Any]:
        """轮询扫码结果，登录成功后自动校验并持久化保存 Token 与用户信息。"""
        pending_cookies: Dict[str, str] = {}
        if os.path.exists(PENDING_LOGIN_FILE):
            try:
                with open(PENDING_LOGIN_FILE, "r", encoding="utf-8") as f:
                    p = json.load(f)
                if not security_id:
                    security_id = p.get("security_id")
                pending_cookies = p.get("cookies") or {}
            except Exception:
                pass

        if not security_id:
            raise ValueError("未提供 security_id 且未找到待扫码记录，请先调用 dm_login_qrcode 获取登录二维码。")

        poll_sess = self._make_http_session()
        for k, v in pending_cookies.items():
            poll_sess.cookies.set(k, v, domain="www.5idream.net", path="/")

        wait_seconds = max(0, min(int(wait_seconds), 180))
        deadline = time.time() + wait_seconds
        last_data: Dict[str, Any] = {}

        while True:
            resp = poll_sess.post(
                f"{BASE_URL}/scan_login/rollPoling",
                params={"securityId": security_id, "callback": "cb"},
                timeout=15,
            )
            data = self._parse_jsonp(resp.text)
            last_data = data
            code = str(data.get("code", ""))
            result = data.get("result")

            # login.js 判断条件: "dummy" != e.result && 500 != e.code && 90000017 != e.code
            if (
                result
                and isinstance(result, str)
                and result != "dummy"
                and code not in ("500", "90000017")
            ):
                token = result.strip()
                return self.set_token_and_verify(token, extra_cookies={c.name: c.value for c in poll_sess.cookies})

            if time.time() >= deadline:
                return {
                    "logged_in": False,
                    "status": "waiting_scan",
                    "security_id": security_id,
                    "last_response": last_data,
                    "message": "尚未检测到扫码确认（请使用【到梦空间】APP 扫描二维码并在手机上点击确认登录，随后再次调用 dm_check_login）。",
                }
            time.sleep(interval)

    def set_token_and_verify(
        self,
        token: str,
        extra_cookies: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """设置 dmkj_web_token 并通过 /token/checkToken 验证及补全学校信息。"""
        self.http.cookies.clear()
        if extra_cookies:
            for k, v in extra_cookies.items():
                self.http.cookies.set(k, v, domain="www.5idream.net", path="/")
        self.token = token.strip()
        self.http.cookies.set("dmkj_web_token", self.token, domain="www.5idream.net", path="/")

        resp = self._request("GET", "/token/checkToken", params={"token": self.token}, check_redirect=False)
        body = resp.json()
        if str(body.get("code")) != "100" or not isinstance(body.get("result"), dict):
            self.token = ""
            raise NotLoggedInError(f"Token 校验失败: {body}")

        self.userinfo = dict(body["result"])
        self._enrich_school_info()
        if self.userinfo.get("schoolid"):
            self.http.cookies.set("schoolId", str(self.userinfo["schoolid"]), domain="www.5idream.net", path="/")
        self._save_session_to_disk()
        return {
            "logged_in": True,
            "userinfo": self.userinfo,
        }

    def _enrich_school_info(self) -> None:
        """从已签到或已报名活动中自动提取学生的学校与学院信息。"""
        uid = self.userinfo.get("id")
        if not uid:
            return
        if self.userinfo.get("schoolid") and self.userinfo.get("schoolname"):
            return
        try:
            r = self._request(
                "POST",
                "/activity/activity/mycheckin",
                data={"userid": uid, "page": 1, "rows": 1},
                check_redirect=False,
            )
            j = r.json()
            rows = j.get("rows") or []
            if rows:
                row0 = rows[0]
                if row0.get("schoolid"):
                    self.userinfo["schoolid"] = str(row0["schoolid"])
                if row0.get("schoolname"):
                    self.userinfo["schoolname"] = row0["schoolname"]
                return
        except Exception:
            pass

        try:
            r = self._request(
                "POST",
                "/activity/activity/myjoin",
                data={"userid": uid, "page": 1, "rows": 1},
                check_redirect=False,
            )
            j = r.json()
            rows = j.get("rows") or []
            if rows and rows[0].get("id"):
                det = self.get_school_activity_raw(str(rows[0]["id"]))
                if det:
                    if det.get("schoolid"):
                        self.userinfo["schoolid"] = str(det["schoolid"])
                    if det.get("schoolname"):
                        self.userinfo["schoolname"] = det["schoolname"]
        except Exception:
            pass

    def ensure_logged_in(self) -> Dict[str, Any]:
        """确保当前已登录且 Token 有效，返回 userinfo。"""
        self._load_session_from_disk()
        if not self.token:
            raise NotLoggedInError(
                "当前尚未登录到梦空间。请先调用 dm_login_qrcode 获取登录二维码，使用【到梦空间】APP 扫码后调用 dm_check_login 完成登录。"
            )
        resp = self._request("GET", "/token/checkToken", params={"token": self.token}, check_redirect=False)
        try:
            body = resp.json()
        except Exception as e:
            raise NotLoggedInError(f"登录态校验异常，请重新扫码登录 ({e})")

        if str(body.get("code")) != "100" or not isinstance(body.get("result"), dict):
            raise NotLoggedInError(
                "到梦空间登录态已过期，请重新调用 dm_login_qrcode 获取二维码扫码登录。"
            )
        # 保留已富化的 schoolid / schoolname
        prev_school_id = self.userinfo.get("schoolid")
        prev_school_name = self.userinfo.get("schoolname")
        self.userinfo.update(body["result"])
        if prev_school_id and not self.userinfo.get("schoolid"):
            self.userinfo["schoolid"] = prev_school_id
        if prev_school_name and not self.userinfo.get("schoolname"):
            self.userinfo["schoolname"] = prev_school_name
        if not self.userinfo.get("schoolid"):
            self._enrich_school_info()
            self._save_session_to_disk()
        return self.userinfo

    def get_status_summary(self) -> Dict[str, Any]:
        """获取当前登录状态及个人活动概览统计。"""
        self._load_session_from_disk()
        if not self.token:
            return {"logged_in": False, "message": "未登录，请调用 dm_login_qrcode 获取二维码扫码登录。"}
        try:
            info = self.ensure_logged_in()
        except NotLoggedInError as e:
            return {"logged_in": False, "message": str(e)}

        uid = info["id"]
        counts: Dict[str, int] = {}
        endpoints = {
            "joined_count": "/activity/activity/myjoin",
            "checked_in_count": "/activity/activity/mycheckin",
            "focused_count": "/activity/activity/myfocuse",
            "managed_count": "/activity/activity/mymanage",
            "created_count": "/activity/activity/mycreate",
            "joined_tribes_count": "/tribe/tribe/myjoinlist",
        }

        def _fetch_cnt(key: str, ep: str) -> Tuple[str, int]:
            try:
                r = self._request("POST", ep, data={"userid": uid, "page": 1, "rows": 1})
                return key, int(r.json().get("records") or 0)
            except Exception:
                return key, 0

        with ThreadPoolExecutor(max_workers=6) as pool:
            for k, v in pool.map(lambda kv: _fetch_cnt(kv[0], kv[1]), endpoints.items()):
                counts[k] = v

        return {
            "logged_in": True,
            "user": info,
            "counts": counts,
        }

    # ------------------------------------------------------------------
    # 2. 活动列表与详情查询
    # ------------------------------------------------------------------
    def get_school_activity_raw(self, activity_id: str) -> Optional[Dict[str, Any]]:
        """调用 /activity/activity/activityrepublish 获取校内活动完整原始字典。"""
        uid = self.userinfo.get("id", "")
        resp = self._request(
            "POST",
            "/activity/activity/activityrepublish",
            data={"userid": uid, "activityId": str(activity_id)},
        )
        txt = resp.text.strip()
        if not txt:
            return None
        body = resp.json()
        if str(body.get("code")) == "100" and isinstance(body.get("result"), dict):
            return body["result"]
        return None

    def format_school_activity_detail(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """将 activityrepublish 原始响应标准化为结构清晰的活动详情字典。"""
        max_num_raw = str(raw.get("joinmaxnum") or "0")
        join_num = int(raw.get("joinnum") or 0)
        if max_num_raw == "-1":
            max_num_display = "不限人数"
            remaining_quota: Any = "不限"
        else:
            max_num_int = int(max_num_raw) if max_num_raw.isdigit() else 0
            max_num_display = f"{max_num_int}人" if str(raw.get("joinmethod")) != "2" else f"{max_num_int}个团队"
            remaining_quota = max(0, max_num_int - join_num)

        scores = []
        for s in raw.get("listScore") or []:
            scores.append({
                "account_type": s.get("account_name") or s.get("unit") or "积分",
                "credit_module": s.get("name") or "",
                "amount": float(s.get("unitcount") or 0),
                "amount_str": f"{s.get('unitcount', '0')} {s.get('unit') or s.get('account_name') or '积分'}",
                "unit": s.get("unit") or s.get("account_name") or "积分",
                "max_provide_count": s.get("maxprovidecount"),
            })

        achievements = []
        for a in raw.get("listAchievement") or []:
            achievements.append({
                "group_type": "团队奖项" if str(a.get("groupflag")) == "1" else "个人奖项",
                "item": a.get("item") or a.get("name") or "",
                "reward": a.get("reward") or "",
                "quota": a.get("num") or "",
            })

        managers = [m.get("name") for m in (raw.get("activityManagerList") or []) if m.get("name")]
        teachers = [
            f"{t.get('name', '')}" + (f"（{t.get('post')}）" if t.get("post") else "")
            for t in (raw.get("teacherDtoList") or [])
            if t.get("name")
        ]
        labels = [lb.get("labelname") for lb in (raw.get("activityLabelList") or []) if lb.get("labelname")]
        attachments = [
            {"filename": att.get("filename"), "url": att.get("url"), "mimetype": att.get("mimetype")}
            for att in (raw.get("listAttachment") or [])
        ]
        images = [img.get("url") for img in (raw.get("listimg") or []) if img.get("url")]

        topic_obj = raw.get("activityTopic") or {}
        content_clean = clean_html_text(raw.get("content"))
        jointip_clean = clean_html_text(raw.get("jointip"))

        return {
            "id": str(raw.get("id", "")),
            "name": raw.get("name", ""),
            "status_code": str(raw.get("status", "")),
            "status": format_status(raw.get("status"), raw.get("reviewstatus")),
            "level": LEVEL_MAP.get(str(raw.get("level", "")), str(raw.get("level", ""))),
            "activity_type_1": raw.get("catalog1name") or "",
            "activity_type_2": raw.get("catalog2name") or "",
            "labels": labels,
            "topic": topic_obj.get("topicName") or "",
            "school": raw.get("schoolname") or "",
            "college": raw.get("collegename") or "",
            "tribe_name": raw.get("tribename") or "",
            "tribe_id": str(raw.get("tribeid") or ""),
            "address": raw.get("address") or "暂无",
            "coordinates": (
                f"{raw.get('latitude')},{raw.get('longitude')}"
                if raw.get("latitude") and raw.get("longitude")
                else ""
            ),
            "start_time": fmt_ts(raw.get("startdate")),
            "end_time": fmt_ts(raw.get("enddate")),
            "join_start_time": fmt_ts(raw.get("joinstartdate")),
            "join_end_time": fmt_ts(raw.get("joinenddate")),
            "join_way": JOIN_WAY_MAP.get(str(raw.get("joinway", "")), str(raw.get("joinway", ""))),
            "join_method": JOIN_METHOD_MAP.get(str(raw.get("joinmethod", "")), "个人报名"),
            "join_range": JOIN_RANGE_MAP.get(str(raw.get("joinrange", "")), "不限"),
            "max_participants": max_num_display,
            "max_participants_raw": -1 if max_num_raw == "-1" else (int(max_num_raw) if max_num_raw.isdigit() else 0),
            "enrolled_participants": join_num,
            "remaining_quota": remaining_quota,
            "enroll_limit_max": raw.get("enrollMax"),
            "credits": scores,
            "achievements": achievements,
            "leader": (raw.get("activityManagerVo") or {}).get("name") or raw.get("activityleadername") or "",
            "organizers": managers,
            "advisor_teachers": teachers,
            "content": content_clean,
            "join_notice": jointip_clean,
            "detail_url": f"{BASE_URL}/activity/activitydetail.html?activityid={raw.get('id', '')}",
            "cover_image": raw.get("firstImg") or "",
            "images": images,
            "attachments": attachments,
        }

    def get_my_activities(
        self,
        category: str = "joined",
        status_filter: str = "all",
        keyword: str = "",
        page: int = 1,
        page_size: int = 10,
        include_details: bool = True,
    ) -> Dict[str, Any]:
        """查询我的活动列表（joined/checked_in/focused/managed/created），支持并发补全详情。"""
        info = self.ensure_logged_in()
        uid = info["id"]

        endpoint_map = {
            "joined": ("/activity/activity/myjoin", "我报名的活动"),
            "checked_in": ("/activity/activity/mycheckin", "我签到的活动"),
            "focused": ("/activity/activity/myfocuse", "我关注的活动"),
            "managed": ("/activity/activity/mymanage", "我管理的活动"),
            "created": ("/activity/activity/mycreate", "我发起的活动"),
        }
        if category not in endpoint_map:
            raise ValueError(f"不支持的 category={category}，可选: {list(endpoint_map.keys())}")

        ep, cat_label = endpoint_map[category]
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 50))

        status_code_filter = {
            "enrolling": {"2", "3"},
            "waiting": {"4"},
            "ongoing": {"5"},
            "finished": {"6"},
            "active": {"2", "3", "4", "5"},
        }.get(status_filter)

        kw = (keyword or "").strip().lower()

        # 若带筛选条件，一次拉取较多条在内存精准过滤并分页；否则直接按服务端分页
        if status_code_filter or kw:
            resp = self._request("POST", ep, data={"userid": uid, "page": 1, "rows": 100})
            data = resp.json()
            all_rows = data.get("rows") or []
            filtered = []
            for r in all_rows:
                if status_code_filter and str(r.get("status", "")) not in status_code_filter:
                    continue
                if kw:
                    hay = " ".join([
                        str(r.get("name", "")),
                        str(r.get("tribename", "")),
                        str(r.get("address", "")),
                        str(r.get("catalog1name", "")),
                        str(r.get("catalog2name", "")),
                        str(r.get("catalogname", "")),
                    ]).lower()
                    if kw not in hay:
                        continue
                filtered.append(r)
            total_records = len(filtered)
            start_idx = (page - 1) * page_size
            target_rows = filtered[start_idx : start_idx + page_size]
        else:
            resp = self._request("POST", ep, data={"userid": uid, "page": page, "rows": page_size})
            data = resp.json()
            total_records = int(data.get("records") or 0)
            target_rows = data.get("rows") or []

        items: List[Dict[str, Any]] = []
        if include_details and target_rows:
            detail_map: Dict[str, Dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=min(10, len(target_rows))) as pool:
                fut_map = {
                    pool.submit(self.get_school_activity_raw, str(r.get("id"))): str(r.get("id"))
                    for r in target_rows
                    if r.get("id")
                }
                for fut in as_completed(fut_map):
                    aid = fut_map[fut]
                    try:
                        raw_det = fut.result()
                        if raw_det:
                            detail_map[aid] = self.format_school_activity_detail(raw_det)
                    except Exception:
                        pass

            for r in target_rows:
                aid = str(r.get("id", ""))
                if aid in detail_map:
                    items.append(detail_map[aid])
                else:
                    items.append(self._format_basic_row(r))
        else:
            items = [self._format_basic_row(r) for r in target_rows]

        return {
            "category": category,
            "category_label": cat_label,
            "page": page,
            "page_size": page_size,
            "total_records": total_records,
            "returned_count": len(items),
            "items": items,
        }

    def _format_basic_row(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(r.get("id", "")),
            "name": r.get("name", ""),
            "status_code": str(r.get("status", "")),
            "status": format_status(r.get("status"), r.get("reviewstatus")),
            "activity_type_1": r.get("catalog1name") or r.get("catalogname") or "",
            "activity_type_2": r.get("catalog2name") or r.get("catalogname") or "",
            "address": r.get("address") or "暂无",
            "start_time": fmt_ts(r.get("startdate")),
            "end_time": fmt_ts(r.get("enddate")),
            "join_start_time": fmt_ts(r.get("joinstartdate")),
            "join_end_time": fmt_ts(r.get("joinenddate")),
            "tribe_name": r.get("tribename") or "",
            "detail_url": f"{BASE_URL}/activity/activitydetail.html?activityid={r.get('id', '')}",
        }

    def get_public_activities(
        self,
        keyword: str = "",
        activity_type: Optional[int] = None,
        only_open: bool = True,
        page: int = 1,
        page_size: int = 15,
    ) -> Dict[str, Any]:
        """查询到梦空间全国公开活动大厅 (/public/activity/list)。"""
        # 拉取较宽页以便进行未过期与类型筛选
        fetch_size = max(30, page_size * 2) if (only_open or activity_type) else page_size
        files = {
            "activityName": (None, keyword or ""),
            "page": (None, str(page if not (only_open or activity_type) else 1)),
            "pagesize": (None, str(fetch_size)),
        }
        headers = {"X-Requested-With": "XMLHttpRequest"}
        resp = self._request(
            "POST",
            "/public/activity/list",
            files=files,
            headers=headers,
            check_redirect=False,
        )
        body = resp.json()
        rows = body.get("rows") or []
        now_str = datetime.now(tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        filtered = []
        for r in rows:
            end_str = str(r.get("endDateStr") or "")
            is_open = bool(not end_str or end_str >= now_str)
            if only_open and not is_open:
                continue
            atype = str(r.get("activityType") or "1")
            if activity_type is not None and atype != str(activity_type):
                continue
            filtered.append({
                "id": str(r.get("id", "")),
                "name": r.get("activityName") or "",
                "activity_type": PUBLIC_ACTIVITY_TYPE_MAP.get(atype, "全国活动"),
                "start_time": r.get("startDateStr") or "",
                "end_time": end_str,
                "is_open": is_open,
                "scope": "全国公开活动",
                "detail_hint": f"调用 dm_activity_detail(activity_id='{r.get('id', '')}', is_national=True) 查看完整内容",
            })

        if only_open or activity_type:
            total = len(filtered)
            start = (max(1, page) - 1) * page_size
            paged = filtered[start : start + page_size]
        else:
            total = int(body.get("records") or len(filtered))
            paged = filtered

        return {
            "total_matched": total,
            "page": page,
            "page_size": page_size,
            "items": paged,
        }

    def get_public_activity_detail(self, activity_id: str) -> Optional[Dict[str, Any]]:
        """查询全国公开活动详情 (/public/activity/detail)。"""
        files = {"id": (None, str(activity_id))}
        resp = self._request("POST", "/public/activity/detail", files=files, check_redirect=False)
        body = resp.json()
        if str(body.get("code")) != "100" or not isinstance(body.get("result"), dict):
            return None
        res = body["result"]
        atype = str(res.get("activityType") or "1")
        return {
            "id": str(res.get("id", activity_id)),
            "name": res.get("activityName") or "",
            "scope": "全国公开活动",
            "activity_type": PUBLIC_ACTIVITY_TYPE_MAP.get(atype, "全国活动"),
            "start_time": res.get("startDateStr") or "",
            "end_time": res.get("endDateStr") or "",
            "abstract": clean_html_text(res.get("activityAbstract")),
            "content": clean_html_text(res.get("description")),
            "cover_image": res.get("activityCover") or "",
            "detail_url": f"{BASE_URL}/public/activity/list.html",
        }

    def get_available_activities(
        self,
        scope: str = "all",
        keyword: str = "",
        activity_type: Optional[int] = None,
        only_open: bool = True,
        page: int = 1,
        page_size: int = 15,
    ) -> Dict[str, Any]:
        """综合查询可报名的活动（校内关注/报名中活动 + 全国公开可报名活动）。"""
        result: Dict[str, Any] = {"scope": scope}

        if scope in ("all", "school"):
            try:
                info = self.ensure_logged_in()
                uid = info["id"]
                seen_ids = set()
                school_candidates: List[Tuple[str, Dict[str, Any]]] = []
                # 聚合 我关注的 / 我报名的 / 我管理的 / 我发起的 中处于规划中(2)或报名中(3)的活动
                sources = [
                    ("我关注的", "/activity/activity/myfocuse"),
                    ("我已报名的(报名期)", "/activity/activity/myjoin"),
                    ("我管理的", "/activity/activity/mymanage"),
                    ("我发起的", "/activity/activity/mycreate"),
                ]
                for src_label, ep in sources:
                    r = self._request("POST", ep, data={"userid": uid, "page": 1, "rows": 50})
                    for row in r.json().get("rows") or []:
                        st = str(row.get("status", ""))
                        if only_open and st not in ("2", "3"):
                            continue
                        aid = str(row.get("id", ""))
                        if aid and aid not in seen_ids:
                            if keyword:
                                hay = f"{row.get('name','')} {row.get('tribename','')} {row.get('address','')}".lower()
                                if keyword.lower() not in hay:
                                    continue
                            seen_ids.add(aid)
                            school_candidates.append((src_label, row))

                school_items = []
                if school_candidates:
                    with ThreadPoolExecutor(max_workers=min(8, len(school_candidates))) as pool:
                        futs = {
                            pool.submit(self.get_school_activity_raw, str(row.get("id"))): (src_label, row)
                            for src_label, row in school_candidates
                        }
                        for fut in as_completed(futs):
                            src_label, row = futs[fut]
                            raw_det = fut.result()
                            item = (
                                self.format_school_activity_detail(raw_det)
                                if raw_det
                                else self._format_basic_row(row)
                            )
                            item["relation"] = src_label
                            school_items.append(item)
                    school_items.sort(key=lambda x: x.get("id", ""), reverse=True)

                result["school_enrolling"] = {
                    "school_name": info.get("schoolname") or "",
                    "count": len(school_items),
                    "items": school_items,
                    "note": (
                        "到梦空间网页端(www.5idream.net)仅开放【我关注的/我的活动】中的校内活动详情与名额查询；"
                        "若需追踪任意校内新活动，在到梦空间 App 点击【关注】后即可在此实时查询剩余名额、时间地点与积分详情。"
                    ),
                }
            except NotLoggedInError as e:
                result["school_enrolling"] = {"logged_in": False, "message": str(e), "items": []}

        if scope in ("all", "national"):
            result["national_public"] = self.get_public_activities(
                keyword=keyword,
                activity_type=activity_type,
                only_open=only_open,
                page=page,
                page_size=page_size,
            )

        return result

    # ------------------------------------------------------------------
    # 3. 个人学分 / 积分统计（按学分类型统计 & 按活动类型统计）
    # ------------------------------------------------------------------
    def _fetch_all_rows(self, endpoint: str, userid: str, page_size: int = 100) -> List[Dict[str, Any]]:
        all_rows: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self._request("POST", endpoint, data={"userid": userid, "page": page, "rows": page_size})
            data = resp.json()
            rows = data.get("rows") or []
            all_rows.extend(rows)
            total_records = int(data.get("records") or 0)
            if not rows or len(all_rows) >= total_records or page >= 20:
                break
            page += 1
        return all_rows

    def get_school_credit_modules(self, school_id: Optional[str] = None) -> Dict[str, Any]:
        """获取学校全部二课活动分类目录 (/activity/all/catalog) 及对应学分模块 (/activity/activity/schoolBaseSpecialAch)。"""
        info = self.ensure_logged_in()
        sid = str(school_id or info.get("schoolid") or "")
        cat_resp = self._request("GET", "/activity/all/catalog")
        catalogs = cat_resp.json() if cat_resp.text.strip() else []

        second_ids: List[Tuple[str, str, str]] = []
        for c1 in catalogs or []:
            c1_name = c1.get("name") or ""
            for c2 in c1.get("childList") or []:
                second_ids.append((str(c2.get("mid")), c1_name, c2.get("name") or c1_name))

        modules: List[Dict[str, Any]] = []
        if sid and second_ids:
            def _fetch_mod(item: Tuple[str, str, str]) -> List[Dict[str, Any]]:
                cid, c1_name, c2_name = item
                try:
                    r = self._request(
                        "POST",
                        "/activity/activity/schoolBaseSpecialAch",
                        data={"catalogid": cid, "schoolId": sid},
                    )
                    rows = (r.json() if r.text.strip() else {}).get("rows") or []
                    for row in rows:
                        row["catalog1_name"] = c1_name
                        row["catalog2_name"] = c2_name
                    return rows
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=7) as pool:
                for sub in pool.map(_fetch_mod, second_ids):
                    modules.extend(sub)

        return {
            "school_id": sid,
            "school_name": info.get("schoolname") or (modules[0].get("schoolname") if modules else ""),
            "activity_catalogs": catalogs,
            "credit_modules": modules,
        }

    def get_credit_summary(
        self,
        source: str = "checked_in",
        only_finished: bool = False,
    ) -> Dict[str, Any]:
        """统计个人积分/学分信息：包含【按学分类型统计】（账户维度 + 学分模块维度）与【按活动类型统计】。"""
        info = self.ensure_logged_in()
        uid = info["id"]

        ep = "/activity/activity/mycheckin" if source == "checked_in" else "/activity/activity/myjoin"
        rows = self._fetch_all_rows(ep, uid, page_size=100)
        if only_finished:
            rows = [r for r in rows if str(r.get("status")) == "6"]

        # 并发获取所有活动的完整学分配置 + 学校标准分类/学分模块 + 成绩单生成记录
        details_by_id: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=12) as pool:
            fut_map = {
                pool.submit(self.get_school_activity_raw, str(r.get("id"))): str(r.get("id"))
                for r in rows
                if r.get("id")
            }
            fut_meta = pool.submit(self.get_school_credit_modules, info.get("schoolid"))
            fut_score_cert = pool.submit(
                self._request, "POST", "/certificate/certificate/scorelist", data={"userid": uid, "page": 1, "rows": 20}
            )
            fut_rec_cert = pool.submit(
                self._request, "POST", "/certificate/certificate/recordlist", data={"userid": uid, "page": 1, "rows": 20}
            )

            for fut in as_completed(fut_map):
                aid = fut_map[fut]
                try:
                    det = fut.result()
                    if det:
                        details_by_id[aid] = det
                except Exception:
                    pass

            school_meta = fut_meta.result()
            try:
                score_certs = fut_score_cert.result().json().get("rows") or []
            except Exception:
                score_certs = []
            try:
                record_certs = fut_rec_cert.result().json().get("rows") or []
            except Exception:
                record_certs = []

        # 初始化统计桶（预置学校所有官方学分模块与活动分类，方便展示 0 分项）
        by_credit_account: Dict[str, Dict[str, Any]] = {}
        by_credit_module: Dict[str, Dict[str, Any]] = {}
        by_activity_type: Dict[str, Dict[str, Any]] = {}

        def _make_bucket(label: str, unit: str = "积分") -> Dict[str, Any]:
            return {
                "name": label,
                "unit": unit,
                "total_activities": 0,
                "finished_activities": 0,
                "ongoing_or_pending_activities": 0,
                "finished_base_credits": 0.0,
                "finished_max_credits": 0.0,
                "pending_base_credits": 0.0,
                "pending_max_credits": 0.0,
                "total_base_credits": 0.0,
                "total_max_credits": 0.0,
            }

        for mod in school_meta.get("credit_modules") or []:
            mname = mod.get("name") or ""
            uname = mod.get("unitname") or "积分"
            if mname and mname not in by_credit_module:
                by_credit_module[mname] = _make_bucket(mname, uname)
            if uname and uname not in by_credit_account:
                by_credit_account[uname] = _make_bucket(uname, uname)

        for cat in school_meta.get("activity_catalogs") or []:
            cname = cat.get("name") or ""
            if cname and cname not in by_activity_type:
                by_activity_type[cname] = _make_bucket(cname, "积分")

        activity_breakdown: List[Dict[str, Any]] = []
        finished_count = 0
        pending_count = 0

        for r in rows:
            aid = str(r.get("id", ""))
            det = details_by_id.get(aid) or {}
            st = str(det.get("status") or r.get("status") or "")
            is_finished = st == "6"
            if is_finished:
                finished_count += 1
            else:
                pending_count += 1

            act_type = (
                det.get("catalog1name")
                or r.get("catalog1name")
                or det.get("catalog2name")
                or r.get("catalog2name")
                or r.get("catalogname")
                or "未分类"
            )
            act_type_sub = det.get("catalog2name") or r.get("catalog2name") or act_type
            list_score = det.get("listScore") or []

            # 同一活动中同一 (account_name, module_name) 若存在多个档位（例如观众 1分/1000人、工作人员 3分/200人），
            # 单个学生在该活动中只会获得其中一个档位，不能重复相加！
            grouped_tiers: Dict[Tuple[str, str], List[Tuple[float, int]]] = {}
            for sc in list_score:
                acc = sc.get("account_name") or sc.get("unit") or "积分"
                mod_name = sc.get("name") or act_type
                val = float(sc.get("unitcount") or 0.0)
                quota = int(sc.get("maxprovidecount") or 0) if str(sc.get("maxprovidecount", "")).isdigit() else 0
                grouped_tiers.setdefault((acc, mod_name), []).append((val, quota))

            act_base_sum = 0.0
            act_max_sum = 0.0
            credit_desc_parts: List[str] = []

            for (acc, mod_name), tiers in grouped_tiers.items():
                # 常规参与档：优先名额最大的普通参与档（若名额相同取最低保底档），最高档取最大值
                tiers_sorted_by_val = sorted(tiers, key=lambda x: x[0])
                base_val = tiers_sorted_by_val[0][0]
                max_val = tiers_sorted_by_val[-1][0]
                act_base_sum += base_val
                act_max_sum += max_val

                if len(tiers) == 1 or abs(base_val - max_val) < 1e-6:
                    credit_desc_parts.append(f"{mod_name}: {base_val:.2f}{acc}")
                else:
                    tier_vals = "/".join(f"{v:.2f}" for v, _ in tiers_sorted_by_val)
                    credit_desc_parts.append(f"{mod_name}: {tier_vals}{acc}(多档)")

                # 累加至【按学分账户统计】
                b_acc = by_credit_account.setdefault(acc, _make_bucket(acc, acc))
                b_acc["total_activities"] += 1
                if is_finished:
                    b_acc["finished_activities"] += 1
                    b_acc["finished_base_credits"] = round(b_acc["finished_base_credits"] + base_val, 2)
                    b_acc["finished_max_credits"] = round(b_acc["finished_max_credits"] + max_val, 2)
                else:
                    b_acc["ongoing_or_pending_activities"] += 1
                    b_acc["pending_base_credits"] = round(b_acc["pending_base_credits"] + base_val, 2)
                    b_acc["pending_max_credits"] = round(b_acc["pending_max_credits"] + max_val, 2)
                b_acc["total_base_credits"] = round(b_acc["finished_base_credits"] + b_acc["pending_base_credits"], 2)
                b_acc["total_max_credits"] = round(b_acc["finished_max_credits"] + b_acc["pending_max_credits"], 2)

                # 累加至【按学分类型/模块统计】
                b_mod = by_credit_module.setdefault(mod_name, _make_bucket(mod_name, acc))
                b_mod["total_activities"] += 1
                if is_finished:
                    b_mod["finished_activities"] += 1
                    b_mod["finished_base_credits"] = round(b_mod["finished_base_credits"] + base_val, 2)
                    b_mod["finished_max_credits"] = round(b_mod["finished_max_credits"] + max_val, 2)
                else:
                    b_mod["ongoing_or_pending_activities"] += 1
                    b_mod["pending_base_credits"] = round(b_mod["pending_base_credits"] + base_val, 2)
                    b_mod["pending_max_credits"] = round(b_mod["pending_max_credits"] + max_val, 2)
                b_mod["total_base_credits"] = round(b_mod["finished_base_credits"] + b_mod["pending_base_credits"], 2)
                b_mod["total_max_credits"] = round(b_mod["finished_max_credits"] + b_mod["pending_max_credits"], 2)

            # 累加至【按活动类型统计】
            b_act = by_activity_type.setdefault(act_type, _make_bucket(act_type, "积分"))
            b_act["total_activities"] += 1
            if is_finished:
                b_act["finished_activities"] += 1
                b_act["finished_base_credits"] = round(b_act["finished_base_credits"] + act_base_sum, 2)
                b_act["finished_max_credits"] = round(b_act["finished_max_credits"] + act_max_sum, 2)
            else:
                b_act["ongoing_or_pending_activities"] += 1
                b_act["pending_base_credits"] = round(b_act["pending_base_credits"] + act_base_sum, 2)
                b_act["pending_max_credits"] = round(b_act["pending_max_credits"] + act_max_sum, 2)
            b_act["total_base_credits"] = round(b_act["finished_base_credits"] + b_act["pending_base_credits"], 2)
            b_act["total_max_credits"] = round(b_act["finished_max_credits"] + b_act["pending_max_credits"], 2)

            activity_breakdown.append({
                "id": aid,
                "name": det.get("name") or r.get("name") or "",
                "status": format_status(st, det.get("reviewstatus") or r.get("reviewstatus")),
                "is_finished": is_finished,
                "activity_type": act_type if act_type == act_type_sub else f"{act_type} - {act_type_sub}",
                "start_time": fmt_ts(det.get("startdate") or r.get("startdate")),
                "end_time": fmt_ts(det.get("enddate") or r.get("enddate")),
                "tribe_name": det.get("tribename") or r.get("tribename") or "",
                "base_credits": round(act_base_sum, 2),
                "max_credits": round(act_max_sum, 2),
                "credit_detail": "；".join(credit_desc_parts) if credit_desc_parts else "无学分配置",
            })

        return {
            "user": {
                "id": uid,
                "name": info.get("name", ""),
                "school_name": school_meta.get("school_name") or info.get("schoolname", ""),
            },
            "source": "已签到活动 (mycheckin)" if source == "checked_in" else "已报名活动 (myjoin)",
            "overview": {
                "total_activities": len(rows),
                "finished_activities": finished_count,
                "ongoing_or_pending_activities": pending_count,
                "by_credit_account": list(by_credit_account.values()),
            },
            "by_credit_type": list(by_credit_module.values()),
            "by_activity_type": list(by_activity_type.values()),
            "transcripts": {
                "score_transcripts": score_certs,
                "record_transcripts": record_certs,
            },
            "activities": activity_breakdown,
            "note": (
                "部分竞赛/大型活动设有多档学分（如普通观众档与工作人员/获奖高档分），"
                "网页端按单活动展示所有档位配置，因此同时统计了【常规保底分(base)】与【最高档分(max)】。"
            ),
        }

    # ------------------------------------------------------------------
    # 4. 部落、目录与公告查询
    # ------------------------------------------------------------------
    def get_my_tribes(self) -> Dict[str, Any]:
        info = self.ensure_logged_in()
        uid = info["id"]
        r_join = self._request("POST", "/tribe/tribe/myjoinlist", data={"userid": uid, "page": 1, "rows": 50})
        r_mng = self._request("POST", "/tribe/tribe/mymanagelist", data={"userid": uid, "page": 1, "rows": 50})
        return {
            "joined_tribes": (r_join.json() if r_join.text.strip() else {}).get("rows") or [],
            "managed_tribes": (r_mng.json() if r_mng.text.strip() else {}).get("rows") or [],
        }

    def get_notices_and_news(self, limit: int = 10) -> Dict[str, Any]:
        r_notice = self._request("POST", "/yuny/notice/noticelist", data={"page": 1, "rows": limit}, check_redirect=False)
        r_news = self._request("POST", "/yuny/news/newslist", data={"page": 1, "rows": limit}, check_redirect=False)
        notices = [
            {
                "id": str(x.get("id", "")),
                "title": x.get("title", ""),
                "date": fmt_ts(x.get("createdate")),
                "url": x.get("url") or f"{BASE_URL}/yuny/newsdetail.html?id={x.get('id', '')}&type=1",
            }
            for x in ((r_notice.json() if r_notice.text.strip() else {}).get("rows") or [])[:limit]
        ]
        news = [
            {
                "id": str(x.get("id", "")),
                "title": x.get("title", ""),
                "date": fmt_ts(x.get("createdate")),
                "url": x.get("url") or f"{BASE_URL}/yuny/newsdetail.html?id={x.get('id', '')}&type=0",
            }
            for x in ((r_news.json() if r_news.text.strip() else {}).get("rows") or [])[:limit]
        ]
        return {"notices": notices, "news": news}
