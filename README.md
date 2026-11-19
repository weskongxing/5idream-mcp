# 5idream-mcp (到梦空间 MCP Server)

基于标准 MCP (`JSON-RPC 2.0 over stdio`) 协议开发的 **到梦空间（第二课堂成绩单网络管理系统 `https://www.5idream.net`）** 工具服务。无需启动笨重的浏览器自动化（Playwright/Selenium），纯 HTTP 会话直连 + 多线程并发聚合，轻量快速。

## 功能特性

- **App 扫码登录**：生成标准登录二维码（同时返回 MCP `image` 图像块、本地 PNG 文件及终端 Unicode 字符二维码），支持轮询等待手机端确认并自动持久化登录会话。
- **可报名活动查询**：支持检索全国公开可参与活动（全国活动、全国赛事、培训、实践、实习）以及校内关注/报名期活动的实时名额、地点与学分配置。
- **我的活动管理**：支持查询 **我报名的 / 我签到的 / 我关注的 / 我管理的 / 我发起的** 活动列表，并发补全每场活动的参加时间、报名起止时间、地点、人数上限、已报名人数、剩余名额、积分设置与内容摘要。
- **单活动深度详情**：查询活动级别、分类、时间、坐标地点、报名规则、学分/积分与奖项明细、负责人/组织者/指导老师、完整介绍、参与须知与附件链接。
- **第二课堂积分/学分统计**：自动汇总输出 **按学分类型（学校二课学分模块）统计**、**按活动类型统计**、成绩单生成记录及每场活动学分明细，智能处理单活动多档位积分范围。
- **部落与校园资讯**：查询我加入/管理的部落、学校二课活动分类与学分模块映射、到梦空间官方公告与资讯。

## 工具列表

| 工具名 | 说明 |
| :--- | :--- |
| `dm_login_qrcode` | 获取到梦空间扫码登录二维码（PNG 图像 + 本地文件路径 + 终端字符画 + `security_id`） |
| `dm_check_login` | 轮询等待手机【到梦空间】App 扫码确认并持久化保存登录会话 |
| `dm_status` | 查看当前登录状态、学生/学校信息与活动统计概览（支持退出登录或手动导入 Token） |
| `dm_available_activities` | 查询可报名的活动（校内关注/报名中活动 + 全国公开活动大厅） |
| `dm_my_activities` | 查询我的活动列表（`joined` / `checked_in` / `focused` / `managed` / `created`） |
| `dm_activity_detail` | 查询单个活动的完整详情（支持校内活动与全国公开活动） |
| `dm_credit_summary` | 统计个人积分/学分（含按学分类型统计、按活动类型统计及活动学分明细表） |
| `dm_tribes_and_notices` | 查询我的部落、学校二课分类与学分模块体系、官方公告与资讯 |

## 安装依赖

```bash
pip install -r requirements.txt
```

## MCP 配置示例

在 `mcp.json` / Claude Code / Cursor / Magpie / Pi 中添加：

```json
{
  "mcpServers": {
    "5idream": {
      "command": "python",
      "args": ["-u", "/path/to/5idream-mcp/server.py"]
    }
  }
}
```

## 使用流程

1. 调用 `dm_login_qrcode` 获取登录二维码（Windows 下可传 `open_image: true` 自动弹出二维码图片）。
2. 打开手机 **【到梦空间】App** 扫描二维码并点击确认登录。
3. 调用 `dm_check_login` 完成登录校验，登录态将自动保存在本地 `session.json`（已被 `.gitignore` 忽略）。
4. 即可调用 `dm_my_activities`、`dm_available_activities`、`dm_activity_detail`、`dm_credit_summary` 等工具。
