#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到梦空间 (5idream.net) MCP Server — JSON-RPC 2.0 over stdio.

提供工具：
  1. dm_login_qrcode         获取扫码登录二维码（返回 PNG 图像 + 本地文件路径 + 终端字符画，支持自动打开图片）
  2. dm_check_login          轮询等待扫码确认结果并持久化保存登录会话
  3. dm_status               查看当前登录状态、学生与学校信息及活动计数（支持退出登录或手动设置 Token）
  4. dm_available_activities 查询可报名的活动（含校内我关注/报名中的活动详情与剩余名额 + 全国公开活动大厅）
  5. dm_my_activities        查询我的活动列表（已报名/已签到/我关注的/我管理的/我发起的），含时间、地点、名额、学分与内容摘要
  6. dm_activity_detail      查询单个活动的完整详情（时间、地点、全文内容、人数上限、已报人数、积分/学分、奖项、负责人、附件）
  7. dm_credit_summary       个人积分/学分统计汇总（含按学分类型统计、按活动类型统计、学校全模块对照及每场活动学分明细）
  8. dm_tribes_and_notices   查询我的部落、学校二课分类与学分模块配置、到梦空间公告与资讯
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from client import DreamSpaceClient, NotLoggedInError

client = DreamSpaceClient()


def _format_activity_card_md(it: Dict[str, Any], idx: int) -> str:
    """将富化后的活动字典格式化为清晰易读的 Markdown 条目。"""
    lines = [f"### {idx}. {it.get('name', '未命名活动')} (ID: `{it.get('id', '')}`)"]
    meta_parts = [f"**状态**: {it.get('status', '')}"]
    if it.get("relation"):
        meta_parts.append(f"**关联**: {it['relation']}")
    if it.get("level"):
        meta_parts.append(f"**级别**: {it['level']}")
    t1, t2 = it.get("activity_type_1", ""), it.get("activity_type_2", "")
    if t1 or t2:
        meta_parts.append(f"**活动类型**: {t1}" + (f" / {t2}" if t2 and t2 != t1 else ""))
    lines.append("- " + " ｜ ".join(meta_parts))
    lines.append(f"- **参加时间**: {it.get('start_time', '暂无')} 至 {it.get('end_time', '暂无')}")
    if it.get("join_start_time") and it.get("join_start_time") != "暂无":
        lines.append(f"- **报名时间**: {it.get('join_start_time')} 至 {it.get('join_end_time')}")
    loc = it.get("address", "暂无")
    tribe = it.get("tribe_name", "")
    lines.append(f"- **活动地点**: {loc}" + (f" ｜ **主办部落**: {tribe}" if tribe else ""))

    if "enrolled_participants" in it:
        lines.append(
            f"- **报名人数**: 已报 **{it.get('enrolled_participants', 0)}** / 上限 **{it.get('max_participants', '未知')}** "
            f"（剩余名额: {it.get('remaining_quota', '未知')} ｜ 方式: {it.get('join_way', '')} · {it.get('join_method', '')} · {it.get('join_range', '')}）"
        )

    credits = it.get("credits") or []
    if credits:
        c_strs = [
            f"{c['credit_module']} `{c['amount']:.2f}` {c['account_type']}(限{c['max_provide_count']}人)"
            for c in credits
        ]
        lines.append(f"- **积分/学分**: {' ； '.join(c_strs)}")

    content = (it.get("content") or "").strip()
    if content:
        preview = content.replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:160] + "..."
        lines.append(f"- **内容摘要**: {preview}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Tool Handlers
# ----------------------------------------------------------------------
def tool_login_qrcode(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    open_image = bool(args.get("open_image", False))
    qr = client.create_login_qrcode()
    opened_msg = ""
    if open_image and sys.platform.startswith("win"):
        try:
            os.startfile(qr["png_path"])
            opened_msg = "\n- 已自动在系统图片查看器中弹出二维码窗口，可直接扫码。"
        except Exception as e:
            opened_msg = f"\n- 自动弹出图片失败（可点击路径查看）: {e}"

    file_uri = "file:///" + qr["png_path"].replace("\\", "/")
    text = (
        "## 到梦空间扫码登录二维码已生成\n"
        f"- **security_id**: `{qr['security_id']}`\n"
        f"- **生成时间**: {qr['created_at']}\n"
        f"- **本地二维码图片**: [{os.path.basename(qr['png_path'])}]({file_uri}) (`{qr['png_path']}`){opened_msg}\n"
        "- **操作步骤**:\n"
        "  1. 打开手机【到梦空间】APP → 使用「扫一扫」扫描下方二维码图片并在手机端点击确认登录；\n"
        "  2. 扫码后调用 `dm_check_login`（支持自动轮询等待确认）完成登录态保存。\n\n"
        "```\n"
        f"{qr['ascii_qr']}\n"
        "```"
    )
    return [
        {"type": "image", "data": qr["png_base64"], "mimeType": "image/png"},
        {"type": "text", "text": text},
    ]


def tool_check_login(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    sec_id = args.get("security_id")
    wait_sec = int(args.get("wait_seconds", 60))
    res = client.poll_login(security_id=sec_id, wait_seconds=wait_sec)
    if res.get("logged_in"):
        u = res.get("userinfo") or {}
        txt = (
            "## 登录成功！会话已持久化保存\n"
            f"- **姓名**: {u.get('name', '未知')}\n"
            f"- **用户ID**: `{u.get('id', '')}`\n"
            f"- **所属学校**: {u.get('schoolname', '未知')} (schoolId: `{u.get('schoolid', '')}`)\n"
            "- 后续所有查询工具将自动复用该登录态。"
        )
    else:
        txt = (
            "## 暂未完成扫码确认\n"
            f"- **security_id**: `{res.get('security_id', '')}`\n"
            f"- **提示**: {res.get('message', '')}"
        )
    return [{"type": "text", "text": txt}]


def tool_status(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    if args.get("logout"):
        client.clear_session()
        return [{"type": "text", "text": "已退出登录并清除本地到梦空间会话缓存。"}]

    if args.get("token"):
        res = client.set_token_and_verify(str(args["token"]))
        u = res["userinfo"]
        return [{
            "type": "text",
            "text": f"Token 验证通过并已保存！当前用户：{u.get('name')} (`{u.get('id')}`) - {u.get('schoolname', '')}",
        }]

    st = client.get_status_summary()
    if not st.get("logged_in"):
        return [{"type": "text", "text": f"**当前状态**: 未登录\n\n{st.get('message', '')}"}]

    u = st["user"]
    c = st["counts"]
    txt = (
        "## 到梦空间当前登录状态：已登录\n"
        f"- **姓名**: {u.get('name', '')}\n"
        f"- **用户ID**: `{u.get('id', '')}`\n"
        f"- **学校**: {u.get('schoolname', '未知')} (`{u.get('schoolid', '')}`)\n"
        f"- **我报名的活动**: {c.get('joined_count', 0)} 场\n"
        f"- **我签到的活动**: {c.get('checked_in_count', 0)} 场\n"
        f"- **我关注的活动**: {c.get('focused_count', 0)} 场\n"
        f"- **我管理/发起的活动**: {c.get('managed_count', 0)} / {c.get('created_count', 0)} 场\n"
        f"- **我加入的部落**: {c.get('joined_tribes_count', 0)} 个"
    )
    return [{"type": "text", "text": txt}]


def tool_available_activities(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    scope = str(args.get("scope", "all"))
    keyword = str(args.get("keyword", "") or "")
    act_type = args.get("activity_type")
    act_type_int = int(act_type) if act_type is not None and str(act_type).strip() != "" else None
    only_open = bool(args.get("only_open", True))
    page = int(args.get("page", 1))
    page_size = int(args.get("page_size", 15))

    data = client.get_available_activities(
        scope=scope,
        keyword=keyword,
        activity_type=act_type_int,
        only_open=only_open,
        page=page,
        page_size=page_size,
    )

    sections: List[str] = ["## 可报名活动查询结果"]

    if "school_enrolling" in data:
        se = data["school_enrolling"]
        sections.append(f"\n### 一、校内报名中活动（{se.get('school_name') or '当前账号关联'}，共 {se.get('count', 0)} 场）")
        if se.get("logged_in") is False:
            sections.append(f"> {se.get('message')}")
        elif se.get("items"):
            for i, it in enumerate(se["items"], 1):
                sections.append(_format_activity_card_md(it, i))
        else:
            sections.append("- 当前【我关注的 / 我报名的】列表中暂无处于报名期的校内活动。")
        if se.get("note"):
            sections.append(f"\n> 说明：{se['note']}")

    if "national_public" in data:
        np_data = data["national_public"]
        items = np_data.get("items") or []
        sections.append(
            f"\n### 二、全国公开可参与活动（匹配 {np_data.get('total_matched', 0)} 场，当前第 {np_data.get('page', 1)} 页）"
        )
        if items:
            for i, it in enumerate(items, 1):
                sections.append(
                    f"{i}. **{it['name']}** (全国活动ID: `{it['id']}` ｜ 类型: {it['activity_type']})\n"
                    f"   - 活动时间: {it['start_time']} 至 {it['end_time']} ｜ 状态: {'可参与/进行中' if it['is_open'] else '已截止'}\n"
                    f"   - 查看详情: `dm_activity_detail(activity_id=\"{it['id']}\", is_national=True)`"
                )
        else:
            sections.append("- 未检索到符合条件的全国公开活动。")

    return [{"type": "text", "text": "\n".join(sections)}]


def tool_my_activities(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    category = str(args.get("category", "joined"))
    status_filter = str(args.get("status_filter", "all"))
    keyword = str(args.get("keyword", "") or "")
    page = int(args.get("page", 1))
    page_size = int(args.get("page_size", 10))
    include_details = bool(args.get("include_details", True))

    data = client.get_my_activities(
        category=category,
        status_filter=status_filter,
        keyword=keyword,
        page=page,
        page_size=page_size,
        include_details=include_details,
    )

    lines = [
        f"## {data['category_label']}（共 {data['total_records']} 场，第 {data['page']} 页 / 本页 {data['returned_count']} 场）"
    ]
    if not data["items"]:
        lines.append("\n暂无符合条件的活动记录。")
    else:
        for i, it in enumerate(data["items"], 1 + (data["page"] - 1) * data["page_size"]):
            lines.append("")
            lines.append(_format_activity_card_md(it, i))

    return [{"type": "text", "text": "\n".join(lines)}]


def tool_activity_detail(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    activity_id = str(args.get("activity_id", "")).strip()
    if not activity_id:
        raise ValueError("请提供 activity_id")
    is_national = bool(args.get("is_national", False))

    if not is_national:
        try:
            client.ensure_logged_in()
            raw = client.get_school_activity_raw(activity_id)
        except NotLoggedInError:
            raw = None
        if raw:
            d = client.format_school_activity_detail(raw)
            credits = d.get("credits") or []
            credit_lines = (
                "\n".join(
                    f"  - **{c['credit_module']}**: `{c['amount']:.2f}` {c['account_type']}（发放上限 {c['max_provide_count']} 人）"
                    for c in credits
                )
                if credits
                else "  - 暂无学分/积分设置"
            )
            achs = d.get("achievements") or []
            ach_lines = (
                "\n".join(f"  - [{a['group_type']}] {a['item']}: {a['reward']}（名额 {a['quota']}）" for a in achs)
                if achs
                else "  - 暂无奖项设置"
            )
            atts = d.get("attachments") or []
            att_lines = (
                "\n".join(f"  - {a['filename']}: {a['url']}" for a in atts)
                if atts
                else "  - 无附件"
            )
            txt = (
                f"## {d['name']} (ID: `{d['id']}`)\n"
                f"- **当前状态**: {d['status']} ｜ **活动级别**: {d['level']}\n"
                f"- **活动分类**: {d['activity_type_1']} — {d['activity_type_2']}"
                + (f" ｜ **标签**: {', '.join(d['labels'])}" if d["labels"] else "")
                + "\n"
                + (f"- **所属专题**: {d['topic']}\n" if d["topic"] else "")
                + f"- **学校/学院**: {d['school']} {d['college']} ｜ **主办部落**: {d['tribe_name']} (`{d['tribe_id']}`)\n"
                f"- **参加时间**: {d['start_time']} 至 {d['end_time']}\n"
                f"- **报名时间**: {d['join_start_time']} 至 {d['join_end_time']}\n"
                f"- **活动地点**: {d['address']}" + (f" (坐标: `{d['coordinates']}`)" if d["coordinates"] else "") + "\n"
                f"- **报名统计**: 已报名 **{d['enrolled_participants']}** / 上限 **{d['max_participants']}**（剩余名额: {d['remaining_quota']}）\n"
                f"- **报名规则**: {d['join_way']} ｜ {d['join_method']} ｜ 参与范围: {d['join_range']}\n"
                f"- **负责人**: {d['leader'] or '暂无'}"
                + (f" ｜ **组织者**: {', '.join(d['organizers'])}" if d["organizers"] else "")
                + (f" ｜ **指导老师**: {', '.join(d['advisor_teachers'])}" if d["advisor_teachers"] else "")
                + "\n"
                f"- **活动链接**: {d['detail_url']}\n\n"
                f"### 积分 / 学分设置\n{credit_lines}\n\n"
                f"### 奖项设置\n{ach_lines}\n\n"
                f"### 活动内容介绍\n{d['content'] or '暂无'}\n\n"
                + (f"### 参与须知\n{d['join_notice']}\n\n" if d["join_notice"] else "")
                + f"### 活动附件\n{att_lines}"
            )
            return [{"type": "text", "text": txt}]

    # 全国公开活动详情（显式指定 is_national=True 或校内未查到时自动回退）
    pub = client.get_public_activity_detail(activity_id)
    if pub:
        txt = (
            f"## [全国公开活动] {pub['name']} (ID: `{pub['id']}`)\n"
            f"- **活动类型**: {pub['activity_type']}\n"
            f"- **活动时间**: {pub['start_time']} 至 {pub['end_time']}\n"
            f"- **封面图**: {pub['cover_image']}\n\n"
            f"### 活动摘要\n{pub['abstract'] or '暂无'}\n\n"
            f"### 活动详情内容\n{pub['content'] or '暂无'}"
        )
        return [{"type": "text", "text": txt}]

    return [{
        "type": "text",
        "text": (
            f"未查询到活动 ID `{activity_id}` 的详情。\n"
            "注意：校内活动在网页端需处于【我报名的 / 我签到的 / 我关注的 / 我管理的】列表中方可读取详情；"
            "若是全国活动请确认 ID 是否正确。"
        ),
    }]


def tool_credit_summary(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = str(args.get("source", "checked_in"))
    only_finished = bool(args.get("only_finished", False))
    include_list = bool(args.get("include_activity_list", True))

    summary = client.get_credit_summary(source=source, only_finished=only_finished)
    u = summary["user"]
    ov = summary["overview"]

    lines = [
        f"## {u['name']}（{u['school_name']}）第二课堂积分/学分统计",
        f"- **统计口径**: {summary['source']}" + ("（仅已完结活动）" if only_finished else "（含已完结与进行中活动）"),
        f"- **活动场次**: 共 **{ov['total_activities']}** 场（已完结 **{ov['finished_activities']}** 场 ｜ 进行中/待完结 **{ov['ongoing_or_pending_activities']}** 场）",
    ]

    for acc in ov.get("by_credit_account") or []:
        if abs(acc["total_base_credits"] - acc["total_max_credits"]) < 1e-6:
            lines.append(
                f"- **{acc['name']}总计**: **{acc['total_base_credits']:.2f}** {acc['unit']} "
                f"（已完结: {acc['finished_base_credits']:.2f} ｜ 进行中: {acc['pending_base_credits']:.2f}）"
            )
        else:
            lines.append(
                f"- **{acc['name']}总计**: 常规保底 **{acc['total_base_credits']:.2f}** ~ 最高档 **{acc['total_max_credits']:.2f}** {acc['unit']} "
                f"（已完结: {acc['finished_base_credits']:.2f}~{acc['finished_max_credits']:.2f} ｜ "
                f"进行中: {acc['pending_base_credits']:.2f}~{acc['pending_max_credits']:.2f}）"
            )

    def _fmt_range(base_v: float, max_v: float) -> str:
        return f"{base_v:.2f}" if abs(base_v - max_v) < 1e-6 else f"{base_v:.2f} ~ {max_v:.2f}"

    lines.append("\n### 一、按学分类型统计（学校二课学分模块）")
    lines.append("| 学分类型(模块) | 单位 | 总场次 | 已完结学分 | 进行中学分 | 累计学分 |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for row in summary["by_credit_type"]:
        lines.append(
            f"| {row['name']} | {row['unit']} | {row['total_activities']} | "
            f"{_fmt_range(row['finished_base_credits'], row['finished_max_credits'])} | "
            f"{_fmt_range(row['pending_base_credits'], row['pending_max_credits'])} | "
            f"**{_fmt_range(row['total_base_credits'], row['total_max_credits'])}** |"
        )

    lines.append("\n### 二、按活动类型统计（活动一级分类）")
    lines.append("| 活动类型 | 总场次 | 已完结场次 | 已完结积分/学分 | 进行中积分/学分 | 累计积分/学分 |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for row in summary["by_activity_type"]:
        lines.append(
            f"| {row['name']} | {row['total_activities']} | {row['finished_activities']} | "
            f"{_fmt_range(row['finished_base_credits'], row['finished_max_credits'])} | "
            f"{_fmt_range(row['pending_base_credits'], row['pending_max_credits'])} | "
            f"**{_fmt_range(row['total_base_credits'], row['total_max_credits'])}** |"
        )

    sc_certs = summary["transcripts"]["score_transcripts"]
    rc_certs = summary["transcripts"]["record_transcripts"]
    lines.append(
        f"\n### 三、已生成第二课堂成绩单记录\n"
        f"- 学分版成绩单: {len(sc_certs)} 份 ｜ 记录版成绩单: {len(rc_certs)} 份"
    )
    for c in sc_certs[:5]:
        lines.append(f"  - [学分版] 编号 `{c.get('no')}`: {c.get('previewcerturl', '')}")
    for c in rc_certs[:5]:
        lines.append(f"  - [记录版] 编号 `{c.get('no')}`: {c.get('previewcerturl', '')}")

    if include_list and summary["activities"]:
        lines.append("\n### 四、活动积分/学分明细列表")
        lines.append("| # | 活动ID | 活动名称 | 状态 | 活动类型 | 积分/学分配置 | 活动时间 |")
        lines.append("| :---: | :---: | :--- | :---: | :---: | :--- | :---: |")
        for idx, act in enumerate(summary["activities"], 1):
            lines.append(
                f"| {idx} | `{act['id']}` | {act['name']} | {act['status']} | "
                f"{act['activity_type']} | {act['credit_detail']} | {act['start_time']} |"
            )

    lines.append(f"\n> 注：{summary['note']}")
    return [{"type": "text", "text": "\n".join(lines)}]


def tool_tribes_and_notices(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    qtype = str(args.get("query_type", "tribes"))
    if qtype == "tribes":
        data = client.get_my_tribes()
        lines = ["## 我的部落"]
        lines.append(f"\n### 我加入的部落（{len(data['joined_tribes'])} 个）")
        for t in data["joined_tribes"]:
            lines.append(
                f"- **{t.get('name')}** (ID: `{t.get('id')}` ｜ 成员: {t.get('memberno', 0)}人) — {t.get('description') or '暂无简介'}"
            )
        lines.append(f"\n### 我管理的部落（{len(data['managed_tribes'])} 个）")
        if data["managed_tribes"]:
            for t in data["managed_tribes"]:
                lines.append(
                    f"- **{t.get('name')}** (ID: `{t.get('id')}` ｜ 成员: {t.get('memberno', 0)}人) — {t.get('description') or '暂无简介'}"
                )
        else:
            lines.append("- 暂无管理的部落")
        return [{"type": "text", "text": "\n".join(lines)}]

    if qtype == "catalog":
        meta = client.get_school_credit_modules()
        lines = [f"## 学校二课活动分类与学分模块配置（{meta.get('school_name') or '当前学校'}）"]
        lines.append("\n### 活动分类目录")
        for c1 in meta.get("activity_catalogs") or []:
            subs = ", ".join(f"{c2.get('name')}(`{c2.get('mid')}`)" for c2 in (c1.get("childList") or []))
            lines.append(f"- **{c1.get('name')}** (`{c1.get('mid')}`): {subs}")
        lines.append("\n### 学校学分模块映射")
        for m in meta.get("credit_modules") or []:
            lines.append(
                f"- 学分模块 **{m.get('name')}** (ID: `{m.get('id')}` ｜ 账户单位: {m.get('unitname')}) ← 对应分类: {m.get('catalog1_name')}"
            )
        return [{"type": "text", "text": "\n".join(lines)}]

    if qtype == "notices":
        limit = int(args.get("limit", 10))
        nn = client.get_notices_and_news(limit=limit)
        lines = ["## 到梦空间官方公告与校园资讯", "\n### 最新公告"]
        for n in nn["notices"]:
            lines.append(f"- [{n['date']}] **{n['title']}** — {n['url']}")
        lines.append("\n### 最新资讯")
        for n in nn["news"]:
            lines.append(f"- [{n['date']}] **{n['title']}** — {n['url']}")
        return [{"type": "text", "text": "\n".join(lines)}]

    raise ValueError(f"未知 query_type={qtype}，可选: tribes, catalog, notices")


# ----------------------------------------------------------------------
# MCP Tool Registry & JSON-RPC 2.0 Stdio Dispatcher
# ----------------------------------------------------------------------
TOOLS: Dict[str, Dict[str, Any]] = {
    "dm_login_qrcode": {
        "description": (
            "获取到梦空间 (5idream.net) 扫码登录二维码。返回 PNG 二维码图像、本地图片文件路径、终端字符二维码以及 security_id。"
            "用户使用手机【到梦空间】APP 扫码确认后，请调用 dm_check_login 完成登录保存。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "open_image": {
                    "type": "boolean",
                    "description": "是否在 Windows 本机自动用系统图片查看器弹出二维码图片方便用户直接扫码（默认 false）",
                }
            },
        },
        "handler": tool_login_qrcode,
    },
    "dm_check_login": {
        "description": (
            "检查并轮询等待到梦空间扫码登录结果。若用户在指定等待秒数内在手机端确认登录，"
            "将自动获取并持久化保存 dmkj_web_token 与学生/学校信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "security_id": {
                    "type": "string",
                    "description": "可选，dm_login_qrcode 返回的 security_id；留空则自动读取最近一次生成的二维码记录",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "最长轮询等待秒数（默认 60 秒，范围 0~180；传 0 则只检查一次立即返回）",
                },
            },
        },
        "handler": tool_check_login,
    },
    "dm_status": {
        "description": "查看当前到梦空间登录状态、学生姓名/ID/所属学校以及已报名、已签到、关注活动与部落数量概览；也支持退出登录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "logout": {
                    "type": "boolean",
                    "description": "传 true 则退出当前账号并清除本地保存的登录态",
                },
                "token": {
                    "type": "string",
                    "description": "可选，直接传入已有的 dmkj_web_token 进行验证和保存",
                },
            },
        },
        "handler": tool_status,
    },
    "dm_available_activities": {
        "description": (
            "查询可报名的活动。包含两部分：1) 校内处于报名期的活动（从我关注的/我的活动中提取并自动补全参加时间、报名时间、地点、"
            "报名人数上限、已报人数、剩余名额、学分设置与内容摘要）；2) 全国公开可参与的活动/赛事/培训/实践/实习大厅。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["all", "school", "national"],
                    "description": "查询范围：all(全部，默认)、school(仅校内关注/报名期活动)、national(仅全国公开活动大厅)",
                },
                "keyword": {
                    "type": "string",
                    "description": "按活动名称/部落/地点关键词过滤",
                },
                "activity_type": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4, 5],
                    "description": "全国公开活动细分类型：1=全国活动, 2=全国赛事, 3=培训, 4=实践, 5=实习",
                },
                "only_open": {
                    "type": "boolean",
                    "description": "是否仅返回当前未截止/报名中的活动（默认 true）",
                },
                "page": {"type": "integer", "description": "页码，默认 1"},
                "page_size": {"type": "integer", "description": "每页数量，默认 15"},
            },
        },
        "handler": tool_available_activities,
    },
    "dm_my_activities": {
        "description": (
            "查询我的到梦空间活动列表（支持：已报名 joined、已签到 checked_in、我关注的 focused、我管理的 managed、我发起的 created）。"
            "默认并发拉取每场活动的完整详情，直接返回参加时间、报名时间、地点、报名人数上限、已报名人数、积分/学分设置及活动内容摘要。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["joined", "checked_in", "focused", "managed", "created"],
                    "description": "列表类别：joined(我报名的，默认)、checked_in(我签到的)、focused(我关注的)、managed(我管理的)、created(我发起的)",
                },
                "status_filter": {
                    "type": "string",
                    "enum": ["all", "enrolling", "waiting", "ongoing", "finished", "active"],
                    "description": "状态过滤：all(全部，默认)、enrolling(报名中)、waiting(待开始)、ongoing(进行中)、finished(已结束)、active(未结束的全部)",
                },
                "keyword": {
                    "type": "string",
                    "description": "关键词筛选（匹配活动名称、主办部落、地点、活动分类）",
                },
                "page": {"type": "integer", "description": "页码，默认 1"},
                "page_size": {"type": "integer", "description": "每页条数，默认 10（最大 50）"},
                "include_details": {
                    "type": "boolean",
                    "description": "是否并发查询每场活动的名额上限、已报人数、积分与内容摘要（默认 true）",
                },
            },
        },
        "handler": tool_my_activities,
    },
    "dm_activity_detail": {
        "description": (
            "查询单个活动的完整详情，包括参加时间、报名起止时间、活动地点与坐标、完整活动内容、参与须知、"
            "报名人数上限、已报名人数、剩余名额、积分/学分设置、奖项设置、负责人/组织者/指导老师及活动附件（支持校内活动与全国公开活动）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {
                    "type": "string",
                    "description": "活动 ID（如校内活动 '16965802' 或全国公开活动 '490'）",
                },
                "is_national": {
                    "type": "boolean",
                    "description": "是否为全国公开活动 ID（默认 false，若校内未命中也会自动回退尝试全国公开活动）",
                },
            },
            "required": ["activity_id"],
        },
        "handler": tool_activity_detail,
    },
    "dm_credit_summary": {
        "description": (
            "统计个人到梦空间第二课堂积分/学分信息，输出完整的：【按学分类型统计】（含学分账户总额与学校各二课学分模块统计）、"
            "【按活动类型统计】、第二课堂成绩单生成记录以及每场活动的学分获取明细表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["checked_in", "joined"],
                    "description": "统计数据源：checked_in(已签到活动，默认，代表实际出勤参与) 或 joined(已报名活动)",
                },
                "only_finished": {
                    "type": "boolean",
                    "description": "是否仅统计已完结(status=6)的活动（默认 false，同时拆分展示已完结与进行中活动学分）",
                },
                "include_activity_list": {
                    "type": "boolean",
                    "description": "是否在末尾附带每场活动的积分/学分明细表（默认 true）",
                },
            },
        },
        "handler": tool_credit_summary,
    },
    "dm_tribes_and_notices": {
        "description": "查询我的部落（已加入/管理的部落）、学校二课活动分类与学分模块配置，或到梦空间官方公告与校园资讯。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["tribes", "catalog", "notices"],
                    "description": "查询类型：tribes(我的部落，默认)、catalog(学校二课活动分类与学分模块体系)、notices(官方公告与资讯)",
                },
                "limit": {
                    "type": "integer",
                    "description": "当 query_type=notices 时返回的条数上限（默认 10）",
                },
            },
        },
        "handler": tool_tribes_and_notices,
    },
}


def handle_rpc(req: Dict[str, Any]) -> Dict[str, Any] | None:
    rid = req.get("id")
    method = req.get("method")

    if method == "initialize":
        params = req.get("params") or {}
        client_proto = params.get("protocolVersion") or "2024-11-05"
        supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
        proto = client_proto if client_proto in supported else "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "5idream-mcp",
                    "version": "1.0.0",
                },
                "instructions": (
                    "到梦空间(5idream.net)第二课堂助手。支持扫码登录(dm_login_qrcode + dm_check_login)、"
                    "可报名活动与我的活动查询、活动人数/时间/地点/内容/学分详情查询，以及按学分类型与按活动类型的个人学分统计。"
                ),
            },
        }

    if method and method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        tool_list = [
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
            for name, spec in TOOLS.items()
        ]
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if not spec:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"未知工具: {name}"},
            }
        try:
            content = spec["handler"](args)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": False}}
        except NotLoggedInError as e:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": f"[未登录提示] {e}"}],
                    "isError": True,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": f"[执行异常] {type(e).__name__}: {e}"}],
                    "isError": True,
                },
            }

    if rid is not None:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def run_stdio() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_rpc(req)
        except Exception as e:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {type(e).__name__}: {e}"},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


class _McpHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[5idream-mcp] " + (fmt % args) + "\n")

    def _send_bytes(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8", extra_headers: Dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_bytes(200, b'{"status":"ok","server":"5idream-mcp"}')
            return
        # Streamable HTTP: 405 indicates no standalone server-initiated SSE stream needed
        self._send_bytes(405, b'{"error":"Method Not Allowed"}', extra_headers={"Allow": "POST, DELETE"})

    def do_DELETE(self) -> None:
        self._send_bytes(200, b"{}")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ("/mcp", "/mcp/", "/"):
            self._send_bytes(404, b'{"error":"Not Found"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        session_id = self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex
        headers = {"Mcp-Session-Id": session_id}

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}}
            self._send_bytes(400, json.dumps(err, ensure_ascii=False).encode("utf-8"), extra_headers=headers)
            return

        if isinstance(payload, list):
            responses = []
            for item in payload:
                r = handle_rpc(item)
                if r is not None:
                    responses.append(r)
            if not responses:
                self._send_bytes(202, b"", content_type="text/plain; charset=utf-8", extra_headers=headers)
            else:
                self._send_bytes(200, json.dumps(responses, ensure_ascii=False).encode("utf-8"), extra_headers=headers)
            return

        resp = handle_rpc(payload)
        if resp is None:
            self._send_bytes(202, b"", content_type="text/plain; charset=utf-8", extra_headers=headers)
        else:
            self._send_bytes(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), extra_headers=headers)


def run_http(host: str, port: int) -> None:
    srv = ThreadingHTTPServer((host, port), _McpHttpHandler)
    sys.stderr.write(f"[5idream-mcp] Streamable HTTP listening on http://{host}:{port}/mcp\n")
    sys.stderr.flush()
    srv.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="5idream MCP Server (stdio or streamable-http)")
    parser.add_argument("--http", action="store_true", help="Run as Streamable HTTP MCP server")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"), help="HTTP bind host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "18434")), help="HTTP bind port")
    args = parser.parse_args()

    if args.http:
        run_http(args.host, args.port)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
