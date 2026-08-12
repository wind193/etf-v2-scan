#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国债逆回购每日提醒（周一到周五 15:10 云端运行）
推送三通道：Server酱App(sctp弹窗) + Server酱微信(sct) + 163邮箱
"""
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ============================================================
# 交易日判断（A股交易日，节假日自动跳过）
# ============================================================
def is_trading_day():
    """拉上证指数日K线：最后一根K线日期==今天 → 今天是A股交易日。
    节假日/周末时最后一根是上一个交易日，天然跳过。接口失败回退周一至周五。"""
    today = datetime.now().strftime("%Y-%m-%d")
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,5,qfq"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        stock = d.get("data", {}).get("sh000001", {})
        klines = stock.get("qfqday") or stock.get("day") or []
        if not klines:
            raise ValueError("无K线数据")
        last_date = str(klines[-1][0])
        print(f"[交易日判断] 上证最新K线日期: {last_date} | 今天: {today}")
        return last_date == today
    except Exception as e:
        print(f"[交易日判断接口失败，回退：周一至周五视为交易日] {e}", file=sys.stderr)
        return datetime.now().weekday() < 5


# ============================================================
# 推送通道（与 etf_v2_scan.py 同逻辑，独立实现避免耦合）
# ============================================================
def push_serverchan(sendkey, title, content):
    """Server酱推送，自动识别 sct(微信) / sctp(App弹窗)"""
    if not sendkey:
        return False
    if sendkey.startswith("sctp"):
        uid = re.match(r"^sctp(\d+)t", sendkey)
        if not uid:
            print("[Server酱推送失败] sctp key 格式无效", file=sys.stderr)
            return False
        url = f"https://{uid.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = urllib.parse.urlencode({
        "title": title,
        "desp": content,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("code") == 0
    except Exception as e:
        print(f"[Server酱推送失败] {e}", file=sys.stderr)
        return False


def push_email(user, password, host, to_addr, subject, html_body):
    """通过 SMTP 发送邮件（发件人/服务器可配置）"""
    if not user or not password:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = user
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, 465, context=ctx) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[邮件推送失败] {e}", file=sys.stderr)
        return False


# ============================================================
# 提醒内容
# ============================================================
def build_content():
    now = datetime.now()
    wd_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = wd_names[now.weekday()]
    tip = ""
    if wd == "周四":
        tip = (
            "\n📌 今天是周四：买1天期逆回购，资金占用到周一，"
            "可享受【3天利息】（周五+周末），本周最划算的一天！"
        )
    elif wd == "周五":
        tip = (
            "\n📌 周五买1天期只算1天利息，资金周末闲置；"
            "若收益率尚可，可考虑 2天期 品种覆盖周末。"
        )
    elif wd in ("周一", "周二", "周三"):
        tip = "\n📌 今天买1天期，明早资金可用，不耽误用钱。"
    content = (
        f"⏰ {now.strftime('%H:%M')} 收盘了，记得买【国债逆回购】！\n"
        f"\n💰 操作：券商App → 国债逆回购\n"
        f"• 沪市 GC001（204001，10万起）\n"
        f"• 深市 R-001（131810，1000元起）\n"
        f"\n⏱️ 交易到 15:30 截止，别错过！"
        f"{tip}"
    )
    return wd, content


def build_email_html(wd, content):
    lines = content.split("\n")
    body = "".join(
        f"<p style='margin:6px 0'>{l}</p>" if l else "<p>&nbsp;</p>"
        for l in lines
    )
    return (
        f"<div style='font-family:Microsoft YaHei,sans-serif;font-size:15px;line-height:1.7'>"
        f"<h3 style='color:#c0392b'>💰 国债逆回购提醒（{wd}）</h3>{body}"
        f"<hr><p style='color:#888;font-size:12px'>ETF V2 自动提醒系统 · 每天 15:10</p></div>"
    )


def main():
    sc_key = os.environ.get("SERVERCHAN_KEY", "")
    sc3_key = os.environ.get("SC3_KEY", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_to = os.environ.get("SMTP_TO", smtp_user)

    wd, content = build_content()

    # 非A股交易日（节假日/周末）跳过推送
    if not is_trading_day():
        print(f"今天（{wd}）不是A股交易日，跳过推送")
        return

    title = f"💰 逆回购提醒（{wd}）"

    # 三通道推送
    if sc3_key:
        ok = push_serverchan(sc3_key, title, content)
        print("📲 已推送到 App" if ok else "⚠️ App 推送失败")
    if sc_key:
        ok = push_serverchan(sc_key, title, content)
        print("📲 已推送到微信" if ok else "⚠️ 微信推送失败")
    if smtp_user and smtp_pass:
        html = build_email_html(wd, content)
        ok = push_email(smtp_user, smtp_pass, smtp_host, smtp_to, title, html)
        print("📧 已推送到邮箱" if ok else "⚠️ 邮件推送失败")


if __name__ == "__main__":
    main()
