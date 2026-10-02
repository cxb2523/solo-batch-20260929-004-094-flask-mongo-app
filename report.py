"""Self-contained HTML reports for the token-lifecycle worker.

Every sweep writes a timestamped report plus ``latest.html`` into the
reports directory. The page uses inline CSS only, so it opens by double
clicking it with no network access.
"""

import os
from html import escape

from events import aggregate
import worker


OUTCOME_LABELS = {
    worker.SUCCESS: "核销成功",
    worker.INVALID: "链接失效（签名错误/已被轮换）",
    worker.EXPIRED: "令牌已过期",
    worker.ALREADY_ACTIVE: "账号已激活（重复点击）",
    worker.NO_USER: "用户不存在",
}

PATH_LABELS = {
    worker.INLINE: "confirm.html 就地内联报错",
    worker.REDIRECT: "统一重定向到 login",
}


def _table(headers, rows):
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">暂无记录</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(sweep_result, event_log, failure_mode, generated_at):
    stats = aggregate(event_log.events())
    lifecycle_names = {
        "token_issued": "签发",
        "token_redeemed": "核销",
        "token_expired": "过期（worker 清理）",
        "confirmed_residue_cleared": "已核销残留清理",
    }
    lifecycle_rows = [
        [escape(lifecycle_names.get(key, key)), stats["lifecycle"].get(key, 0)]
        for key in ("token_issued", "token_redeemed", "token_expired",
                    "confirmed_residue_cleared")
    ]

    # Outcome x path matrix so the two failure paths are distinguishable.
    matrix_rows = []
    reason_paths = stats["reason_paths"]
    for outcome, label in OUTCOME_LABELS.items():
        inline_count = reason_paths.get(f"{outcome}|{worker.INLINE}", 0)
        redirect_count = reason_paths.get(f"{outcome}|{worker.REDIRECT}", 0)
        matrix_rows.append([escape(label), inline_count, redirect_count,
                            inline_count + redirect_count])

    path_rows = [
        [escape(PATH_LABELS[worker.INLINE]),
         stats["attempts_by_path"].get(worker.INLINE, 0)],
        [escape(PATH_LABELS[worker.REDIRECT]),
         stats["attempts_by_path"].get(worker.REDIRECT, 0)],
    ]

    sweep_rows = [
        ["扫描用户数", sweep_result.users_scanned],
        ["本轮过期令牌", sweep_result.expired_tokens],
        ["本轮已核销残留", sweep_result.confirmed_residue],
        ["开始时间（UTC）", escape(str(sweep_result.started_at))],
        ["结束时间（UTC）", escape(str(sweep_result.finished_at))],
    ]

    expired_list = "、".join(escape(e) for e in sweep_result.expired_emails) or "无"
    residue_list = "、".join(escape(e) for e in sweep_result.residue_emails) or "无"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>令牌生命周期巡检报告</title>
<style>
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
       background: #f4f6f8; color: #1f2933; }}
main {{ max-width: 960px; margin: 0 auto; padding: 32px 20px; }}
h1 {{ font-size: 26px; margin-bottom: 4px; }}
h2 {{ font-size: 19px; margin-top: 34px; border-left: 4px solid #2563eb;
      padding-left: 10px; }}
h3 {{ font-size: 15px; margin: 18px 0 8px; }}
.meta {{ color: #64748b; font-size: 13px; }}
.card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
         padding: 18px 22px; margin-top: 12px; }}
.decision {{ background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 6px;
             padding: 10px 14px; margin: 10px 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 14px; }}
th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; }}
th {{ background: #f1f5f9; }}
.muted {{ color: #94a3b8; text-align: center; }}
.pill {{ display: inline-block; background: #2563eb; color: #fff; border-radius: 999px;
         padding: 2px 12px; font-size: 12px; }}
ul {{ margin: 8px 0; }}
code {{ background: #f1f5f9; padding: 1px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<main>
<h1>令牌生命周期巡检报告</h1>
<p class="meta">生成时间（UTC）：{escape(str(generated_at))} ｜ 当前失败处理模式：
<span class="pill">{escape(PATH_LABELS.get(failure_mode, failure_mode))}</span></p>

<h2>一、令牌存储模型：内嵌用户记录，还是独立集合</h2>
<div class="card">
<div class="decision"><strong>取舍决定：</strong>令牌（<code>confirm_token</code>）与过期戳
（<code>confirm_expires_at</code>）<strong>内嵌在 user 用户记录</strong>上，不另建独立集合。
核销时写入 <code>verified=True</code> 与 <code>confirmed_at</code> 并摘除令牌字段。</div>
<ul>
<li><strong>重发与旧链接：</strong>重发即重签并轮换内嵌令牌；旧链接签名虽仍有效，
但与库中最新令牌不匹配，按“链接失效”拒绝，旧链接不会延后到期、也不会复活。</li>
<li><strong>并发双击幂等：</strong>核销用单条条件更新
<code>{{email, confirm_token, verified: False}}</code> 完成；同链接并发双击只有一个请求
匹配成功，另一请求落入“账号已激活”分支，同一条链接全局只生效一次。</li>
<li><strong>孤儿记录回收：</strong>不存在独立集合即无游离令牌文档；worker 每轮把
“已验证但令牌字段残留”和“未验证但令牌过期”两类字段从用户记录上摘除，完成孤儿回收。</li>
</ul>
</div>

<h2>二、过期比较：UTC 还是本地时间，大于还是大于等于</h2>
<div class="card">
<div class="decision"><strong>取舍决定：</strong>存、比一律使用<strong>带时区的 UTC</strong>
（<code>datetime.now(timezone.utc)</code>），判等采用严格大于
<code>now &gt; expires</code> 才算过期。</div>
<ul>
<li>本地时间比较会随部署主机时区漂移，跨时区用户链接寿命会无故缩短或拉长。</li>
<li>边界那一秒 <code>now == expires</code> 仍然有效：若用 <code>&gt;=</code>，
在时间戳取整到同一秒时，刚发出的链接会立刻被判失效；严格大于保证链接至少完整活满 TTL，
也不会出现永不过期。</li>
</ul>
</div>

<h2>三、确认失败的呈现：就地内联，还是统一重定向 login</h2>
<div class="card">
<div class="decision"><strong>取舍决定：</strong>两条路径都保留并在下方<strong>按路径分开统计</strong>；
默认在 <code>confirm.html</code> 就地内联展示具体原因（链接失效 / 账号已激活 / 用户不存在 / 已过期），
设置 <code>CONFIRM_FAILURE_MODE=redirect</code> 时统一带错误码重定向到 <code>login</code>。</div>
<ul>
<li>内联路径保留确认页上下文，用户能区分“过期”与“已激活”等不同原因。</li>
<li>重定向路径把所有失败收敛到登录页，URL 带 <code>confirm_error</code> 错误码。</li>
<li>无论走哪条路径，失败原因都写入审计事件，报告矩阵按“原因 × 路径”分别计数，可区分统计。</li>
</ul>
</div>

<h2>本轮巡检结果</h2>
<div class="card">
{_table(["指标", "值"], sweep_rows)}
<h3>过期令牌对应邮箱</h3><p>{expired_list}</p>
<h3>已核销残留对应邮箱</h3><p>{residue_list}</p>
</div>

<h2>生命周期迁移累计</h2>
<div class="card">
{_table(["迁移", "次数"], lifecycle_rows)}
</div>

<h2>确认结果 × 呈现路径统计</h2>
<div class="card">
{_table(["确认结果", "内联路径", "重定向 login 路径", "合计"], matrix_rows)}
<h3>路径汇总</h3>
{_table(["路径", "尝试次数"], path_rows)}
</div>
</main>
</body>
</html>
"""


def write_report(reports_dir, sweep_result, event_log, failure_mode=worker.INLINE,
                 now=None):
    """Render the report and return the path to the timestamped file."""
    from verification import utcnow

    generated_at = now or utcnow()
    os.makedirs(reports_dir, exist_ok=True)
    html = render_html(sweep_result, event_log, failure_mode, generated_at)

    stamp = generated_at.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(reports_dir, f"report-{stamp}.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    with open(os.path.join(reports_dir, "latest.html"), "w", encoding="utf-8") as handle:
        handle.write(html)
    return path
