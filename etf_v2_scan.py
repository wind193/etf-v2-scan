#!/usr/bin/env python3
"""
ETF V2 动量轮动策略 · 自动扫描脚本
纯 Python 标准库 · 零依赖 · 秒级启动
数据源: 腾讯财经 API  |  推送: Server酱(微信) + SMTP(邮件)
运行方式: 本地 CLI / 腾讯云函数 SCF（云端状态存163邮箱）

用法:
  python etf_v2_scan.py              实盘模式（更新状态+双通道推送）
  python etf_v2_scan.py --dry         干跑模式（不更新状态不推送）
  python etf_v2_scan.py --cloud       云端模式（状态存163邮箱）
  python etf_v2_scan.py --set-serverchan <KEY>    设置Server酱SendKey(微信推送)
  python etf_v2_scan.py --set-email <授权码>      设置163邮箱SMTP(邮件推送)

腾讯云函数入口: main_handler(event, context)
"""

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
import urllib.error
import imaplib
import poplib
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import message_from_bytes
from pathlib import Path

# ============================================================
# 配置参数
# ============================================================
MOMENTUM_WINDOW = 22        # 动量窗口（交易日）
ENTRY_THRESHOLD = 0.025      # 入场门槛 2.5%
COOLDOWN_DAYS = 3           # 冷静期 3 个交易日
BIAS_ABS_THRESHOLD = 0.12   # BIAS 绝对乖离阈值 12%
BIAS_LONG_PCT = 0.99        # 长周期极值分位
BIAS_SHORT_PCT = 0.95       # 短周期极值分位
BIAS_HIT_REQUIRED = 2       # BIAS 过热需命中次数
HISTORY_DAYS = 500          # 请求历史K线根数

# 标的池
ATTACK_ETFS = ["159518", "159915", "159941"]
GOLD_ETF = "518880"
ALL_ETFS = ATTACK_ETFS + [GOLD_ETF]

ETF_NAMES = {
    "159518": "石油ETF",
    "159915": "创业板ETF",
    "159941": "纳指ETF",
    "518880": "黄金ETF",
}

# 文件路径
SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "runtime" / "etf_v2_state.json"
CONFIG_FILE = SCRIPT_DIR / "runtime" / "etf_v2_config.json"


# ============================================================
# 配置管理
# ============================================================
def load_config():
    cfg = {"serverchan_key": "", "email_user": "", "email_pass": "", "email_to": ""}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 环境变量覆盖（GitHub Actions 云端运行使用，不落盘更安全）
    env_map = {
        "SERVERCHAN_KEY": "serverchan_key",
        "SMTP_USER": "email_user",
        "SMTP_PASS": "email_pass",
        "SMTP_TO": "email_to",
    }
    for env_name, key in env_map.items():
        val = os.environ.get(env_name)
        if val:
            cfg[key] = val
    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============================================================
# Server酱推送 — 推送到微信（关注「方糖」公众号即可）
# ============================================================
def push_serverchan(sendkey, title, content):
    """通过 Server酱 推送到微信"""
    if not sendkey:
        return False
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = urllib.parse.urlencode({
        "title": title,
        "desp": content,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("code") == 0
    except Exception as e:
        print(f"[微信推送失败] {e}", file=sys.stderr)
        return False


def format_wechat_content(result):
    """生成微信推送内容（纯文本）"""
    lines = []

    warn = result.get("cloud_state_warning", "")
    if warn:
        lines.append(warn)

    if result["action"] == "switch":
        if result["target"] == GOLD_ETF:
            lines.append(f"🛡️ 切换: {result['current_name']} → {result['target_name']}")
        elif result["target"] == "CASH":
            lines.append(f"💵 切换: {result['current_name']} → 现金")
        else:
            lines.append(f"🔄 切换: {result['current_name']} → {result['target_name']}")
    else:
        lines.append(f"✅ 持有: {result['current_name']}")

    lines.append(f"规则: {result['reason']}")

    for sym, mom in result["attack_ranking"]:
        s = result["signals"].get(sym, {})
        qual = "Y" if s.get("qualified") else "N"
        marker = ">>" if sym == result["attack_ranking"][0][0] else "  "
        curr = " *" if sym == result["current_position"] else ""
        lines.append(f"{marker}{ETF_NAMES.get(sym,'')} {mom:+.1f}% [{qual}]{curr}")

    gs = result["signals"].get(GOLD_ETF)
    if gs and gs.get("momentum") is not None:
        curr = " *" if GOLD_ETF == result["current_position"] else ""
        lines.append(f"  黄金 {gs['momentum']:+.1f}%{curr}")

    if result["all_negative"]:
        lines.append("⚠️ 三只ETF全负!")

    return "\n".join(lines)


# ============================================================
# SMTP 邮件推送 — 直连163邮箱，零确认全自动
# ============================================================
def push_email(user, password, to_addr, subject, html_body):
    """通过 SMTP 发送邮件（163邮箱）"""
    if not user or not password:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = user
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.163.com", 465, context=ctx) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[邮件推送失败] {e}", file=sys.stderr)
        return False


def format_email_html(result):
    """生成邮件HTML内容"""
    warn_line = ""
    warn = result.get("cloud_state_warning", "")
    if warn:
        warn_line = f"<p style='color:#ff4d4f;background:#fff1f0;padding:8px;border-radius:4px'>{warn}</p>"

    action_line = ""
    if result["action"] == "switch":
        if result["target"] == GOLD_ETF:
            action_line = f"<h2 style='color:#d4a017'>🛡️ 切换: {result['current_name']} → {result['target_name']}</h2>"
        elif result["target"] == "CASH":
            action_line = f"<h2 style='color:#888'>💵 切换: {result['current_name']} → 现金</h2>"
        else:
            action_line = f"<h2 style='color:#1890ff'>🔄 切换: {result['current_name']} → {result['target_name']}</h2>"
    else:
        action_line = f"<h2 style='color:#52c41a'>✅ 继续持有 {result['current_name']}</h2>"

    rows = ""
    for i, (sym, mom) in enumerate(result["attack_ranking"]):
        s = result["signals"].get(sym, {})
        bias = s.get("bias_hits", "?")
        qual = "✅" if s.get("qualified") else "❌"
        chg = s.get("today_change")
        chg_s = f"{chg:+.2f}%" if chg is not None else "-"
        marker = "🔥" if i == 0 else ""
        curr = " 📍" if sym == result["current_position"] else ""
        rows += f"<tr><td>{marker}</td><td>{sym}</td><td>{ETF_NAMES.get(sym,'')}</td><td>{mom:+.2f}%</td><td>{bias}</td><td>{qual}</td><td>{chg_s}{curr}</td></tr>"

    gs = result["signals"].get(GOLD_ETF, {})
    gold_row = ""
    if gs and gs.get("momentum") is not None:
        curr = " 📍" if GOLD_ETF == result["current_position"] else ""
        chg = gs.get("today_change")
        chg_s = f"{chg:+.2f}%" if chg is not None else "-"
        gold_row = f"<tr><td></td><td>{GOLD_ETF}</td><td>{ETF_NAMES.get(GOLD_ETF,'')}</td><td>{gs['momentum']:+.2f}%</td><td>-</td><td>🛡️</td><td>{chg_s}{curr}</td></tr>"

    bias_html = ""
    if result["current_state"] == "ATTACK":
        pos_sig = result["signals"].get(result["current_position"], {})
        bd = pos_sig.get("bias_detail", {})
        if bd:
            b22 = bd.get("bias_22")
            b30 = bd.get("bias_30"); p30 = bd.get("pct_30")
            b15 = bd.get("bias_15"); p15 = bd.get("pct_15")
            bias_html = "<h4>🌡 BIAS 详情</h4><table><tr><th>指标</th><th>当前值</th><th>分位</th><th>状态</th></tr>"
            if b22 is not None:
                bias_html += f"<tr><td>BIAS_22</td><td>{b22*100:+.2f}%</td><td>-</td><td>{'⚠️ 过热(>12%)' if bd.get('hit_abs') else '正常'}</td></tr>"
            if b30 is not None:
                p30_s = f"{p30*100:.1f}%" if p30 is not None else "?"
                bias_html += f"<tr><td>BIAS_30</td><td>{b30*100:+.2f}%</td><td>{p30_s}</td><td>{'⚠️ 过热(≥99%)' if bd.get('hit_long') else '正常'}</td></tr>"
            if b15 is not None:
                p15_s = f"{p15*100:.1f}%" if p15 is not None else "?"
                bias_html += f"<tr><td>BIAS_15</td><td>{b15*100:+.2f}%</td><td>{p15_s}</td><td>{'⚠️ 过热(≥95%)' if bd.get('hit_short') else '正常'}</td></tr>"
            bias_html += "</table>"

    warn = "<p style='color:#ff4d4f'>⚠️ 三只ETF 22日动量全部为负</p>" if result["all_negative"] else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;max-width:520px;margin:0 auto;padding:16px;color:#333}}
h2{{margin:0 0 8px 0}}h4{{margin:16px 0 4px 0}}
table{{border-collapse:collapse;width:100%;margin:8px 0}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
th{{background:#f5f5f5}}
p{{margin:4px 0;color:#666}}
</style></head><body>
{warn_line}
{action_line}
<p>📌 {result['reason']}</p>
<p>⏱ 冷静期: {result['cooldown_days']}天 | {'✅已满足' if result['cooldown_met'] else '⏳未满足'}</p>
<h4>📊 22日动量排名</h4>
<table><tr><th></th><th>代码</th><th>名称</th><th>22日动量</th><th>BIAS</th><th>合格</th><th>今日</th></tr>
{rows}{gold_row}</table>
{bias_html}
{warn}
<hr><p>⏰ 下一个交易日 14:25 自动推送 | ETF V2 动量轮动策略</p>
</body></html>"""
# ============================================================
def http_get(url, retries=2, timeout=10):
    """HTTP GET 请求，带重试"""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            print(f"[网络错误] {url[:80]}: {e}", file=sys.stderr)
            return None


def fetch_etf_kline(symbol, count=500):
    """
    从腾讯财经 API 获取 ETF 日K线（前复权）。
    返回: list[dict] 按日期升序排列，每项包含 date/open/close/high/low/volume
    """
    # 判断交易所: 5xxxxx → sh, 其他 → sz
    market = "sh" if symbol.startswith("5") else "sz"
    code = f"{market}{symbol}"
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={code},day,,,{count},qfq"
    )

    data = http_get(url)
    if data is None or data.get("code") != 0:
        return None

    stock_data = data.get("data", {}).get(code)
    if stock_data is None:
        return None

    klines = stock_data.get("qfqday") or stock_data.get("day")
    if not klines:
        return None

    result = []
    for row in klines:
        # 格式: [日期, 开盘, 收盘, 最高, 最低, 成交量]
        if len(row) < 6:
            continue
        result.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]),
        })
    return result


# ============================================================
# 指标计算（纯 Python）
# ============================================================
def calc_momentum(kline_data, window=MOMENTUM_WINDOW):
    """22日动量 = close[-1] / close[-(window+1)] - 1"""
    if len(kline_data) < window + 1:
        return None
    return kline_data[-1]["close"] / kline_data[-(window + 1)]["close"] - 1


def calc_sma(kline_data, n):
    """简单移动平均"""
    if len(kline_data) < n:
        return None
    closes = [k["close"] for k in kline_data[-n:]]
    return sum(closes) / len(closes)


def calc_bias(kline_data, n):
    """BIAS_N = close / SMA_N - 1"""
    if len(kline_data) < n:
        return None
    sma = calc_sma(kline_data, n)
    if sma is None or sma == 0:
        return None
    return kline_data[-1]["close"] / sma - 1


def calc_bias_percentile(kline_data, n, current_value):
    """
    计算 BIAS_N 当前值在历史中的分位。
    遍历所有历史 days，计算每天对应的 BIAS_N，求当前值所处的分位。
    """
    if len(kline_data) < n + 50:  # 至少需要一些历史来算分位
        return None

    bias_values = []
    for i in range(n, len(kline_data)):
        slice_data = kline_data[i - n : i]
        sma = sum(k["close"] for k in slice_data) / n
        if sma != 0:
            bias_values.append(kline_data[i]["close"] / sma - 1)

    if not bias_values:
        return None

    bias_values.sort()
    count_le = sum(1 for v in bias_values if v <= current_value)
    return count_le / len(bias_values)


def check_bias_overheat(kline_data):
    """BIAS 三项检测，返回 (命中数, 详情dict)"""
    hits = 0
    details = {}

    # 1) 绝对乖离 BIAS_22 > 12%
    bias_22 = calc_bias(kline_data, 22)
    details["bias_22"] = bias_22
    if bias_22 is not None and abs(bias_22) > BIAS_ABS_THRESHOLD:
        hits += 1
        details["hit_abs"] = True
    else:
        details["hit_abs"] = False

    # 2) 长周期极值 BIAS_30 >= 99%分位
    bias_30 = calc_bias(kline_data, 30)
    details["bias_30"] = bias_30
    if bias_30 is not None:
        pct_30 = calc_bias_percentile(kline_data, 30, bias_30)
        details["pct_30"] = pct_30
        if pct_30 is not None and pct_30 >= BIAS_LONG_PCT:
            hits += 1
            details["hit_long"] = True
        else:
            details["hit_long"] = False
    else:
        details["hit_long"] = False

    # 3) 短周期极值 BIAS_15 >= 95%分位
    bias_15 = calc_bias(kline_data, 15)
    details["bias_15"] = bias_15
    if bias_15 is not None:
        pct_15 = calc_bias_percentile(kline_data, 15, bias_15)
        details["pct_15"] = pct_15
        if pct_15 is not None and pct_15 >= BIAS_SHORT_PCT:
            hits += 1
            details["hit_short"] = True
        else:
            details["hit_short"] = False
    else:
        details["hit_short"] = False

    return hits, details


# ============================================================
# 状态管理
# ============================================================
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "position": "518880",
        "position_name": "黄金ETF",
        "last_switch_date": None,
        "last_run": None,
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 云端状态存取 — 163邮箱（云函数无本地磁盘，用邮箱当状态库）
#   写入: SMTP 发一封状态邮件给自己（已确认可用）
#   读取: IMAP 搜索主题（首选），失败回退 POP3 遍历
# ============================================================
STATE_MAIL_SUBJECT = "ETF V2 系统状态 [ETFV2STATE]"


def _extract_state_json(msg):
    """从邮件消息中提取状态 JSON 文本"""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode("utf-8", errors="ignore").strip()
                        start = text.find("{")
                        if start >= 0:
                            return text[start:]
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="ignore").strip()
                start = text.find("{")
                if start >= 0:
                    return text[start:]
    except Exception:
        pass
    return None


def imap_save_state(user, password, state):
    """把状态作为邮件发到自己邮箱（SMTP发送）"""
    try:
        msg = MIMEText(json.dumps(state, ensure_ascii=False), "plain", "utf-8")
        msg["Subject"] = STATE_MAIL_SUBJECT
        msg["From"] = user
        msg["To"] = user
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.163.com", 465, context=ctx) as server:
            server.login(user, password)
            server.sendmail(user, [user], msg.as_string())
        return True
    except Exception as e:
        print(f"[状态邮件发送失败] {e}", file=sys.stderr)
        return False


def _imap_load_state(user, password):
    """IMAP 搜索主题读取最新状态邮件"""
    try:
        M = imaplib.IMAP4_SSL("imap.163.com", 993)
        M.login(user, password)
        try:
            typ, _ = M.select("INBOX")
            if typ != "OK":
                return None
            typ, data = M.search(None, '(SUBJECT "ETFV2STATE")')
            if typ != "OK":
                return None
            ids = data[0].split() if data and data[0] else []
            if not ids:
                return None
            typ, msg_data = M.fetch(ids[-1], "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                return None
            raw = msg_data[0][1]
            text = _extract_state_json(message_from_bytes(raw))
            if text:
                return json.loads(text)
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception as e:
        print(f"[IMAP状态读取失败] {e}", file=sys.stderr)
    return None


def _pop3_load_state(user, password):
    """POP3 兜底：从最新往回找状态邮件（最多查最近30封）"""
    try:
        P = poplib.POP3_SSL("pop.163.com", 995)
        P.user(user)
        P.pass_(password)
        try:
            num, _ = P.stat()
            if num <= 0:
                return None
            for i in range(num, max(1, num - 30), -1):
                try:
                    resp, lines, _ = P.top(i, 6)
                except Exception:
                    continue
                head = b"\n".join(lines).decode("utf-8", errors="ignore")
                if "ETFV2STATE" in head:
                    resp, lines, _ = P.retr(i)
                    raw = b"\n".join(lines)
                    text = _extract_state_json(message_from_bytes(raw))
                    if text:
                        return json.loads(text)
        finally:
            try:
                P.quit()
            except Exception:
                pass
    except Exception as e:
        print(f"[POP3状态读取失败] {e}", file=sys.stderr)
    return None


def load_state_cloud(user, password):
    """云端读取状态，返回 (state, found)"""
    st = _imap_load_state(user, password)
    if st is None:
        st = _pop3_load_state(user, password)
    if st and st.get("position"):
        print(f"[云端] 已从邮箱恢复状态: {st.get('position_name')}（{st.get('position')}）")
        return st, True
    print("[云端] 邮箱中无状态记录，使用默认（黄金ETF）", file=sys.stderr)
    return {
        "position": "518880",
        "position_name": "黄金ETF",
        "last_switch_date": None,
        "last_run": None,
    }, False


# ============================================================
# 决策引擎 — V2 完整 12 条规则
# ============================================================
def make_decision(state, all_klines):
    """返回 (action, target, reason, signals, ranking, ...) 完整决策结果"""

    position = state.get("position", "518880")
    last_switch = state.get("last_switch_date")

    # ---- 1. 计算每只 ETF 的信号 ----
    signals = {}
    today_date = None

    for sym in ALL_ETFS:
        kl = all_klines.get(sym)
        if kl is None or len(kl) < MOMENTUM_WINDOW + 1:
            signals[sym] = {"momentum": None, "bias_hits": None, "qualified": False, "error": True}
            continue

        if today_date is None:
            today_date = kl[-1]["date"]

        mom = calc_momentum(kl)
        bias_hits, bias_detail = check_bias_overheat(kl)
        qualified = mom is not None and mom >= ENTRY_THRESHOLD and bias_hits < BIAS_HIT_REQUIRED

        signals[sym] = {
            "momentum": round(mom * 100, 2) if mom is not None else None,
            "bias_hits": bias_hits,
            "bias_detail": bias_detail,
            "qualified": qualified,
            "close": kl[-1]["close"],
        }
        # 当日涨跌
        if len(kl) >= 2:
            signals[sym]["today_change"] = round((kl[-1]["close"] / kl[-2]["close"] - 1) * 100, 2)
        else:
            signals[sym]["today_change"] = None

    # ---- 2. 进攻 ETF 排名（按动量降序）----
    attack_scores = []
    for sym in ATTACK_ETFS:
        s = signals[sym]
        if s["momentum"] is not None:
            attack_scores.append((sym, s["momentum"], s["qualified"]))
    attack_scores.sort(key=lambda x: x[1], reverse=True)

    all_negative = all(s[1] < 0 for s in attack_scores) if attack_scores else True

    # ---- 3. 当前状态和持仓动量 ----
    if position in ATTACK_ETFS:
        current_state = "ATTACK"
    elif position == GOLD_ETF:
        current_state = "GOLD"
    else:
        current_state = "CASH"

    pos_mom = None
    if position in signals and signals[position]["momentum"] is not None:
        pos_mom = signals[position]["momentum"]

    # ---- 4. 合格替补（动量>=2.5% 且 BIAS命中<2，且不是当前持仓）----
    qualified_subs = [(sym, mom) for sym, mom, qual in attack_scores if qual and sym != position]
    best_sub = qualified_subs[0] if qualified_subs else None

    # ---- 5. 冷静期（从最近一次切换日起算交易日）----
    # 用 159915（创业板ETF）的 K 线数据作为交易日历
    calendar_kl = all_klines.get("159915") or []
    calendar_dates = [k["date"] for k in calendar_kl]

    days_held = 0
    if last_switch and last_switch in calendar_dates and today_date in calendar_dates:
        start_idx = calendar_dates.index(last_switch)
        end_idx = calendar_dates.index(today_date)
        days_held = max(0, end_idx - start_idx)

    cooldown_met = days_held >= COOLDOWN_DAYS

    # ---- 6. 决策规则 T1-T12 ----
    action = None
    target = None
    reason = ""

    # T1: 三只ETF全负 → 黄金（风险退出，绕过冷静期）
    if current_state in ("ATTACK", "CASH") and all_negative and position != GOLD_ETF:
        action, target, reason = "switch", GOLD_ETF, "T1 三只ETF全负 → 黄金(风险退出)"

    # T2: 进攻持仓动量<0，有合格替补 → 切入最强替补
    elif current_state == "ATTACK" and pos_mom is not None and pos_mom < 0 and best_sub:
        action, target, reason = "switch", best_sub[0], f"T2 持仓走负+有替补 → {ETF_NAMES[best_sub[0]]}"

    # T3: 进攻持仓动量<0，无合格替补 → 黄金
    elif current_state == "ATTACK" and pos_mom is not None and pos_mom < 0 and not best_sub:
        action, target, reason = "switch", GOLD_ETF, "T3 持仓走负+无替补 → 黄金"

    # T4: BIAS≥2，有合格替补 → 切入最强替补
    elif current_state == "ATTACK" and signals[position].get("bias_hits", 0) >= BIAS_HIT_REQUIRED and best_sub:
        action, target, reason = "switch", best_sub[0], f"T4 BIAS过热+有替补 → {ETF_NAMES[best_sub[0]]}"

    # T5: BIAS≥2，无合格替补 → 现金
    elif current_state == "ATTACK" and signals[position].get("bias_hits", 0) >= BIAS_HIT_REQUIRED and not best_sub:
        action, target, reason = "switch", "CASH", "T5 BIAS过热+无替补 → 现金"

    # T8: 黄金状态，存在合格ETF → 切进攻
    elif current_state == "GOLD" and best_sub:
        action, target, reason = "switch", best_sub[0], f"T8 黄金+存在合格ETF → {ETF_NAMES[best_sub[0]]}"

    # T9: 黄金状态，无合格ETF → 继续持有
    elif current_state == "GOLD" and not best_sub:
        action, target, reason = "hold", position, "T9 黄金+无合格ETF → 继续持有"

    # T10: 现金状态，全负 → 黄金
    elif current_state == "CASH" and all_negative:
        action, target, reason = "switch", GOLD_ETF, "T10 现金+全负 → 黄金"

    # T11: 现金状态，有合格ETF → 切入
    elif current_state == "CASH" and best_sub:
        action, target, reason = "switch", best_sub[0], f"T11 现金+有合格ETF → {ETF_NAMES[best_sub[0]]}"

    # T12: 现金状态，非全负但无合格 → 继续现金
    elif current_state == "CASH" and not all_negative:
        action, target, reason = "hold", position, "T12 现金+无合格 → 继续持有"

    # T6: 进攻持仓，最强合格≠当前，满3天冷静期 → 切换
    elif current_state == "ATTACK" and best_sub and cooldown_met:
        action, target, reason = "switch", best_sub[0], f"T6 满{COOLDOWN_DAYS}天+更强信号 → {ETF_NAMES[best_sub[0]]}"

    # T7: 进攻持仓，未满冷静期或无需切换 → 继续持有
    elif current_state == "ATTACK":
        if not cooldown_met:
            reason = f"T7 冷静期中({days_held}/{COOLDOWN_DAYS}天) → 继续持有"
        else:
            reason = "T7 持仓仍为最优 → 继续持有"
        action, target = "hold", position

    else:
        action, target, reason = "hold", position, "无触发 → 继续持有"

    return {
        "date": today_date or date.today().strftime("%Y-%m-%d"),
        "current_state": current_state,
        "current_position": position,
        "current_name": ETF_NAMES.get(position, "现金"),
        "action": action,
        "target": target,
        "target_name": ETF_NAMES.get(target, "现金"),
        "reason": reason,
        "signals": signals,
        "attack_ranking": [(sym, mom) for sym, mom, _ in attack_scores],
        "cooldown_days": days_held,
        "cooldown_met": cooldown_met,
        "all_negative": all_negative,
    }


# ============================================================
# 输出格式化
# ============================================================
def format_signal(result):
    lines = []

    if result["action"] == "switch":
        if result["target"] == GOLD_ETF:
            lines.append(f"🛡️ 建议切换: {result['current_name']} → {result['target_name']}")
        elif result["target"] == "CASH":
            lines.append(f"💵 建议切换: {result['current_name']} → 现金")
        else:
            lines.append(f"🔄 建议切换: {result['current_name']} → {result['target_name']}")
    else:
        lines.append(f"✅ 继续持有 {result['current_name']}")

    lines.append(f"📌 规则: {result['reason']}")
    lines.append(f"⏱ 冷静期: {result['cooldown_days']}天 | {'✅已满足' if result['cooldown_met'] else '⏳未满足'}")

    # 动量排名
    lines.append("\n📊 22日动量排名:")
    for i, (sym, mom) in enumerate(result["attack_ranking"]):
        s = result["signals"].get(sym, {})
        bias = s.get("bias_hits", "?")
        qual = "✅合格" if s.get("qualified") else "❌"
        chg = s.get("today_change")
        chg_str = f" 今日:{chg:+.2f}%" if chg is not None else ""
        marker = "🔥 " if i == 0 else "  "
        curr = " 📍持仓" if sym == result["current_position"] else ""
        lines.append(f"{marker}{sym} {ETF_NAMES.get(sym,'')} | 22日:{mom:+.2f}% | BIAS命中:{bias} | {qual}{chg_str}{curr}")

    # 黄金
    gs = result["signals"].get(GOLD_ETF, {})
    if gs and gs.get("momentum") is not None:
        curr = " 📍持仓" if GOLD_ETF == result["current_position"] else ""
        lines.append(f"    {GOLD_ETF} {ETF_NAMES.get(GOLD_ETF,'')} | 22日:{gs['momentum']:+.2f}%{curr}")

    # BIAS详情
    if result["current_state"] == "ATTACK":
        pos_sig = result["signals"].get(result["current_position"], {})
        bd = pos_sig.get("bias_detail", {})
        if bd:
            lines.append(f"\n🌡 BIAS详情 ({result['current_name']}):")
            b22 = bd.get("bias_22")
            if b22 is not None:
                lines.append(f"    BIAS_22: {b22*100:+.2f}% {'⚠️ >12%' if bd.get('hit_abs') else ''}")
            b30 = bd.get("bias_30")
            p30 = bd.get("pct_30")
            if b30 is not None:
                p30_s = f"分位:{p30*100:.1f}%" if p30 is not None else "分位:?"
                lines.append(f"    BIAS_30: {b30*100:+.2f}% {p30_s} {'⚠️ ≥99%' if bd.get('hit_long') else ''}")
            b15 = bd.get("bias_15")
            p15 = bd.get("pct_15")
            if b15 is not None:
                p15_s = f"分位:{p15*100:.1f}%" if p15 is not None else "分位:?"
                lines.append(f"    BIAS_15: {b15*100:+.2f}% {p15_s} {'⚠️ ≥95%' if bd.get('hit_short') else ''}")

    if result["all_negative"]:
        lines.append("\n⚠️ 三只ETF 22日动量全部为负")

    return "\n".join(lines)


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="ETF V2 动量策略扫描")
    parser.add_argument("--dry", action="store_true", help="干跑模式，不更新状态不推送")
    parser.add_argument("--push-only", action="store_true", help="仅推送（需预先有数据缓存）")
    parser.add_argument("--set-serverchan", type=str, metavar="KEY", help="设置 Server酱 SendKey（微信推送）")
    parser.add_argument("--set-email", type=str, metavar="CODE", help="设置163邮箱SMTP授权码（邮件推送）")
    args = parser.parse_args()

    # 设置 Server酱
    if args.set_serverchan:
        cfg = load_config()
        cfg["serverchan_key"] = args.set_serverchan
        save_config(cfg)
        print("✅ Server酱 SendKey 已保存")
        ok = push_serverchan(args.set_serverchan, "ETF V2 策略已就绪",
                             "✅ Server酱配置成功\n每日14:25自动推送信号")
        if ok:
            print("✅ 微信推送测试成功，请检查微信")
        else:
            print("⚠️ 微信推送测试失败，请检查 SendKey")
        return

    # 设置邮件
    if args.set_email:
        cfg = load_config()
        cfg["email_user"] = "18201691896@163.com"
        cfg["email_pass"] = args.set_email
        cfg["email_to"] = "18201691896@163.com"
        save_config(cfg)
        print("✅ 163邮箱 SMTP 授权码已保存")
        ok = push_email(cfg["email_user"], cfg["email_pass"], cfg["email_to"],
                        "✅ ETF V2 邮件推送已就绪",
                        "<h2>✅ SMTP 配置成功</h2><p>每日14:25自动推送策略信号到本邮箱</p>")
        if ok:
            print("✅ 邮件推送测试成功，请检查163邮箱")
        else:
            print("⚠️ 邮件推送测试失败，请检查授权码")
        return

def run_scan(cloud=False, dry=False):
    """扫描主流程。cloud=True 状态存163邮箱；dry=True 不更新状态不推送"""
    cfg = load_config()
    sc_key = cfg.get("serverchan_key", "")
    email_user = cfg.get("email_user", "")
    email_pass = cfg.get("email_pass", "")
    email_to = cfg.get("email_to", "")

    if cloud:
        state, state_found = load_state_cloud(email_user, email_pass)
    else:
        state = load_state()
        state_found = True

    print("=" * 55)
    print(f"ETF V2 策略扫描 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {'☁️云端' if cloud else '本地'}")
    print(f"当前持仓: {state['position_name']}（{state['position']}）")
    print(f"上次调仓: {state.get('last_switch_date', '无记录')}")
    print(f"微信推送: {'✅' if sc_key else '❌'}  |  邮件推送: {'✅' if email_pass else '❌'}")
    print("=" * 55)

    # 获取行情数据
    print("\n⏳ 获取行情数据...")
    all_klines = {}
    all_ok = True
    for sym in ALL_ETFS:
        kl = fetch_etf_kline(sym, HISTORY_DAYS)
        if kl and len(kl) >= MOMENTUM_WINDOW + 1:
            all_klines[sym] = kl
            latest = kl[-1]
            print(f"  ✅ {sym} {ETF_NAMES.get(sym,'')} - {len(kl)}根K线 | 最新:{latest['date']} 收盘:{latest['close']:.3f}")
        else:
            all_klines[sym] = None
            print(f"  ❌ {sym} {ETF_NAMES.get(sym,'')} - 获取失败")
            all_ok = False

    if not all_ok:
        print("\n⚠️ 部分数据获取失败，决策可能不完整")

    # 执行决策
    result = make_decision(state, all_klines)
    result["cloud_state_warning"] = ""
    if cloud and not state_found:
        result["cloud_state_warning"] = "⚠️ 云端未找到历史持仓状态，本次按默认处理"

    # 输出信号
    signal_text = format_signal(result)
    print("\n" + signal_text)
    print("\n" + "=" * 55)

    # 更新状态
    if not dry:
        if result["action"] == "switch":
            state["position"] = result["target"]
            state["position_name"] = result["target_name"]
            state["last_switch_date"] = result["date"]
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if cloud:
            if imap_save_state(email_user, email_pass, state):
                print("💾 状态已写入邮箱")
            else:
                print("⚠️ 状态写入邮箱失败", file=sys.stderr)
        else:
            save_state(state)
            print(f"\n💾 状态已保存: {state['position_name']}（{state['position']}）")

    # 双通道推送
    if not dry:
        if sc_key:
            title = f"ETF {'调仓' if result['action'] == 'switch' else ''} {result['current_name']}"
            ok = push_serverchan(sc_key, title, format_wechat_content(result))
            print("📲 已推送到微信" if ok else "⚠️ 微信推送失败")
        if email_pass:
            subject = f"ETF V2 {'⚠️调仓' if result['action'] == 'switch' else '持仓'} · {result['date']}"
            ok = push_email(email_user, email_pass, email_to, subject, format_email_html(result))
            print("📧 已推送到邮箱" if ok else "⚠️ 邮件推送失败")

    return result


# ============================================================
# 主入口（本地 CLI）
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="ETF V2 动量策略扫描")
    parser.add_argument("--dry", action="store_true", help="干跑模式，不更新状态不推送")
    parser.add_argument("--cloud", action="store_true", help="云端模式（状态存163邮箱）")
    parser.add_argument("--set-serverchan", type=str, metavar="KEY", help="设置 Server酱 SendKey（微信推送）")
    parser.add_argument("--set-email", type=str, metavar="CODE", help="设置163邮箱SMTP授权码（邮件推送）")
    args = parser.parse_args()

    # 设置 Server酱
    if args.set_serverchan:
        cfg = load_config()
        cfg["serverchan_key"] = args.set_serverchan
        save_config(cfg)
        print("✅ Server酱 SendKey 已保存")
        ok = push_serverchan(args.set_serverchan, "ETF V2 策略已就绪",
                             "✅ Server酱配置成功\n每日14:25自动推送信号")
        if ok:
            print("✅ 微信推送测试成功，请检查微信")
        else:
            print("⚠️ 微信推送测试失败，请检查 SendKey")
        return

    # 设置邮件
    if args.set_email:
        cfg = load_config()
        cfg["email_user"] = "18201691896@163.com"
        cfg["email_pass"] = args.set_email
        cfg["email_to"] = "18201691896@163.com"
        save_config(cfg)
        print("✅ 163邮箱 SMTP 授权码已保存")
        ok = push_email(cfg["email_user"], cfg["email_pass"], cfg["email_to"],
                        "✅ ETF V2 邮件推送已就绪",
                        "<h2>✅ SMTP 配置成功</h2><p>每日14:25自动推送策略信号到本邮箱</p>")
        if ok:
            print("✅ 邮件推送测试成功，请检查163邮箱")
        else:
            print("⚠️ 邮件推送测试失败，请检查授权码")
        return

    # 正常扫描
    run_scan(cloud=args.cloud, dry=args.dry)


# ============================================================
# 腾讯云函数入口（SCF）
# ============================================================
def main_handler(event, context):
    """腾讯云函数定时触发入口：完整扫描 + 状态写入邮箱 + 双通道推送"""
    print("=== ETF V2 云端扫描启动 ===")
    try:
        result = run_scan(cloud=True, dry=False)
        print("=== 扫描完成 ===")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "action": result["action"],
                "target": result.get("target"),
                "target_name": result.get("target_name"),
                "reason": result["reason"],
                "date": result["date"],
            }, ensure_ascii=False)
        }
    except Exception as e:
        print(f"[云函数异常] {e}", file=sys.stderr)
        try:
            cfg = load_config()
            key = cfg.get("serverchan_key", "")
            if key:
                push_serverchan(key, "⚠️ ETF V2 云端扫描异常", f"错误: {e}")
        except Exception:
            pass
        return {"statusCode": 500, "body": f"error: {e}"}


if __name__ == "__main__":
    main()