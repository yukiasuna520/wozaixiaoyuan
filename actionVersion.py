# -*- coding: utf-8 -*-
"""
我在校园（超星）蓝牙打卡 —— 吉林农业大学版

关键事实（2026-11-12 实测）：
  接口域名 : https://wzxy.chaoxing.com   （不是 gw.wozaixiaoyuan.com）
  登录接口 : POST /basicinfo/mobile/login/username  (schoolId + AES 加密密码)
  签到窗口 : 每天 22:30:00 - 23:00:00
  签到接口 : POST /dormSign/mobile/receive/doSignByDevice
  查询接口 : GET  /dormSign/mobile/receive/getMySignLogs
  必需 cookie: JWSESSION / WZXYSESSION / route（route 缺了会 103 未登录）
  signStatus: 0=未开始  1=可签到  2=已签到

配置来源（优先级从高到低）：
  1. 环境变量（GitHub Actions Secrets）
  2. 同目录 config.local.json（本地测试用，别提交到仓库）

邮件相关环境变量：
  mail_address     发件邮箱
  mail_password    发件邮箱的 SMTP 授权码
  mail_host        SMTP 服务器，如 smtp.qq.com
  receive_mail     收件人，多个用逗号分隔：a@qq.com,b@163.com
  mail_subject_prefix  邮件标题前缀，默认「我在校园」
  notify_every_run 填 yes 时，「今天已签到」也发一封（默认不发）
"""
import base64
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE = "https://wzxy.chaoxing.com"
UA = (
    "Mozilla/5.0 (Linux; Android 10; WLZ-AN00 Build/HUAWEIWLZ-AN00; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 "
    "XWEB/4343 MMWEBSDK/20220903 Mobile Safari/537.36 MMWEBID/4162 "
    "MicroMessenger/8.0.28.2240(0x28001C35) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 miniProgram/wxce6d08f781975d91"
)
HERE = os.path.dirname(os.path.abspath(__file__))

# 最多等多久（分钟）
MAX_WAIT_MIN = int(os.environ.get("max_wait_min", "20"))


def env(name, default=""):
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    try:
        with open(os.path.join(HERE, "config.local.json"), "r", encoding="utf-8") as f:
            local = json.load(f)
        if name in local and str(local[name]).strip() != "":
            return str(local[name]).strip()
    except Exception:
        pass
    return default


# ---------------- 通知 ----------------

def recipients():
    """支持逗号、分号、空格分隔的多个收件人。"""
    raw = env("receive_mail")
    return [x for x in re.split(r"[,;\s]+", raw) if x]


def html_body(rows):
    trs = "".join(
        '<tr>'
        f'<td style="padding:5px 16px 5px 0;color:#888;white-space:nowrap;vertical-align:top">{html.escape(str(k))}</td>'
        f'<td style="padding:5px 0;color:#222;font-family:Consolas,Menlo,monospace">{html.escape(str(v))}</td>'
        '</tr>' for k, v in rows)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        '\'Microsoft YaHei\',sans-serif;font-size:14px;line-height:1.6">'
        f'<table style="border-collapse:collapse">{trs}</table>'
        '<div style="margin-top:14px;color:#aaa;font-size:12px">'
        '由 GitHub Actions 自动发送</div></div>'
    )


def msg_send(title, html_text, plain_text=""):
    """发邮件 + Server酱。任何失败都只打印，不影响打卡结果。"""
    sender = env("mail_address")
    to = recipients()
    if not sender:
        print("[推送] 未配置 mail_address，跳过邮件")
    elif not to:
        print("[推送] 未配置 receive_mail，跳过邮件")
    else:
        ok = False
        for attempt in range(1, 4):
            try:
                import yagmail
                yagmail.SMTP(sender, env("mail_password"), env("mail_host")).send(
                    to if len(to) > 1 else to[0], title, [html_text])
                print(f"[推送] 邮件已发送 -> {', '.join(to)}")
                ok = True
                break
            except Exception as e:
                print(f"[推送] 邮件第 {attempt} 次失败: {type(e).__name__}: {e}")
                if attempt < 3:
                    time.sleep(3)
        if not ok:
            print("[推送] 邮件连续 3 次失败，已放弃（不影响打卡）")

    sct = env("sct_ftqq")
    if sct:
        try:
            requests.get(f"https://sctapi.ftqq.com/{sct}.send?"
                         + urllib.parse.urlencode({"title": title,
                                                   "desp": plain_text or title}),
                         timeout=15)
            print("[推送] Server酱已发送")
        except Exception as e:
            print("[推送] Server酱失败:", e)


def notify(status, rows, extra=""):
    """统一的推送入口：title 带学号，正文是表格，另附纯文本版给 Server酱。"""
    account = env("wzxy_username") or "未知账号"
    prefix = env("mail_subject_prefix", "我在校园")
    title = f"[{status}] {prefix} · {account}"
    plain = "\n".join(f"{k}: {v}" for k, v in rows)
    if extra:
        plain += "\n\n" + extra
    msg_send(title, html_body(rows), plain)


# ---------------- 接口 ----------------

def encrypt(plain, key):
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(str(plain).encode("utf-8"), AES.block_size))).decode()


def get_school_id(school_name):
    r = requests.get(f"{BASE}/basicinfo/mobile/login/getSchoolList",
                     headers={"accept": "application/json", "user-agent": UA}, timeout=20)
    for s in (r.json().get("data") or []):
        if s["name"] == school_name:
            return s["id"]
    raise SystemExit(f"[X] 学校列表里找不到「{school_name}」")


def login(username, password, school_id):
    """用 requests.Session 保持 cookie，route 这个 cookie 是必须的。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://wzxy.chaoxing.com/",
                      "X-Requested-With": "com.tencent.mm"})
    key = (str(username) + "0000000000000000")[:16]
    r = s.post(f"{BASE}/basicinfo/mobile/login/username",
               params={"schoolId": school_id, "username": username,
                       "password": encrypt(password, key)},
               timeout=25)
    try:
        body = r.json()
    except Exception:
        raise SystemExit(f"[X] 登录返回非 JSON: {r.text[:300]}")
    if body.get("code") != 0:
        msg = str(body.get("message", ""))
        notify("登录失败", [("账号", username), ("服务端返回", msg), ("时间", now_str())])
        if "冻结" in msg:
            raise SystemExit(
                f"[X] 账号已被冻结：{msg}\n"
                "    1) 停用定时任务，否则每天都撞一次；\n"
                "    2) 小程序「忘记密码」重置；\n"
                "    3) 重置不了找班级管理员；\n"
                "    4) 解冻后更新 wzxy_password 这个 Secret。")
        raise SystemExit(f"[X] 登录失败：{body}\n    密码错 5 次会冻结，别连续重跑。")
    if "WZXYSESSION" not in s.cookies:
        raise SystemExit("[X] 登录成功但没拿到 WZXYSESSION")
    print(f"[OK] {username} 登录成功，cookie: {list(s.cookies.keys())}")
    return s


def api_headers():
    return {"Host": "wzxy.chaoxing.com", "Accept": "application/json, text/plain, */*",
            "User-Agent": UA, "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "com.tencent.mm", "Referer": "https://wzxy.chaoxing.com/"}


def get_sign_logs(session):
    r = session.get(f"{BASE}/dormSign/mobile/receive/getMySignLogs",
                    headers=api_headers(), params={"page": 1, "size": 10}, timeout=25)
    body = r.json()
    if body.get("code") != 0:
        print(f"[!] 查询签到任务失败: {body}")
        return None      # None 表示「查询失败」，与「没有任务」区分开
    return body.get("data") or []


# ---------------- 时间 ----------------

def fmt(ms):
    """固定按北京时间（UTC+8）显示，避免 GitHub 的 UTC 机器让人看不懂。"""
    if not ms:
        return "-"
    return (datetime.datetime.utcfromtimestamp(int(ms) / 1000)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def local_today(ms=None):
    t = (datetime.datetime.utcfromtimestamp((ms or time.time() * 1000) / 1000)
         + datetime.timedelta(hours=8))
    return t.strftime("%Y-%m-%d")


def now_str():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 业务 ----------------

def find_task(logs):
    now_ms = int(time.time() * 1000)
    today = local_today()
    for it in logs:
        start, end = int(it.get("start") or 0), int(it.get("end") or 0)
        if local_today(start) != today:
            continue
        print(f"[..] 今日任务: {it.get('signTitle')}  窗口 {fmt(start)} -> {fmt(end)}  "
              f"signStatus={it.get('signStatus')}")
        if str(it.get("signStatus")) in ("2", "3"):
            return ("signed", it)
        if now_ms < start:
            return ("pending", it)
        if now_ms <= end:
            return ("open", it)
        return ("expired", it)
    return ("none", None)


def task_rows(task):
    """把任务信息整理成邮件里的几行。"""
    if not task:
        return []
    return [
        ("任务", task.get("signTitle") or "-"),
        ("窗口", f"{fmt(task.get('start'))} ~ {fmt(task.get('end'))}"),
        ("宿舍", f"{task.get('locationName') or '-'}（{task.get('userArea') or '-'}）"),
    ]


def do_blue_sign(session, task):
    dev = (task.get("deviceList") or [{}])[0]
    uuid = dev.get("uuid") or ""
    major = dev.get("major") or ""
    if not uuid:
        raise SystemExit("[X] 任务里没有 deviceList，无法蓝牙打卡")
    payload = {"blue1": [uuid.replace("-", "") + str(major)], "blue2": [uuid]}
    params = {"id": task.get("locationId"), "signId": task.get("signId")}
    print(f"[..] 提交蓝牙打卡: {json.dumps(payload)}")
    r = session.post(f"{BASE}/dormSign/mobile/receive/doSignByDevice",
                     headers=api_headers(), params=params,
                     data=json.dumps(payload), timeout=25)
    print(f"[..] 返回: {r.text[:400]}")
    try:
        j = r.json()
    except Exception:
        return False, r.text[:300]
    return j.get("code") == 0, (j.get("data") or j.get("message") or r.text[:300])


def main():
    username = env("wzxy_username")
    password = env("wzxy_password")
    school = env("school_name", "吉林农业大学")
    if not username or not password:
        raise SystemExit("[X] 缺少 wzxy_username / wzxy_password")

    school_id = get_school_id(school)
    print(f"[OK] schoolId={school_id}  host={BASE}")
    session = login(username, password, school_id)

    deadline = time.time() + MAX_WAIT_MIN * 60
    attempt = 0
    fail_streak = 0

    while True:
        attempt += 1
        logs = get_sign_logs(session)

        if logs is None:                       # 查询接口报错（高峰期常见 500）
            fail_streak += 1
            print(f"[..] 第 {attempt} 次检查：接口异常（连续 {fail_streak} 次），稍后重试")
            if time.time() > deadline:
                notify("查询失败", [
                    ("账号", username),
                    ("结果", f"连续 {fail_streak} 次查询签到任务失败"),
                    ("时间", now_str()),
                ], extra="签到窗口 22:30-23:00，请检查是否已错过。")
                return 1
            time.sleep(30 if fail_streak >= 3 else 60)
            continue

        fail_streak = 0
        state, task = find_task(logs)

        if state == "signed":
            print(f"[OK] 今天已经签到过了（{task.get('signTitle')}），无需重复")
            if env("notify_every_run").lower() in ("yes", "y", "1", "true"):
                notify("今日已签到", [("姓名", task.get("name") or "-"),
                                      ("学号", username)] + task_rows(task)
                       + [("检查时间", now_str())])
            return 0

        if state == "expired":
            print("[!] 今天的窗口已过且未签到")
            notify("打卡失败", [("学号", username), ("原因", "签到窗口已过，未签到")]
                   + task_rows(task) + [("时间", now_str())])
            return 1

        if state == "open":
            ok, detail = do_blue_sign(session, task)
            rows = [("状态", "打卡成功" if ok else "打卡失败"),
                    ("姓名", task.get("name") or "-"),
                    ("学号", username),
                    ("班级", task.get("classes") or "-")] + task_rows(task)
            if ok:
                rows.append(("完成时间", now_str()))
                rows.append(("服务端返回", str(detail)))
                print(f"[OK] {username} 蓝牙打卡成功")
                notify("打卡成功", rows)
                return 0
            rows.append(("失败原因", str(detail)))
            rows.append(("时间", now_str()))
            print(f"[X] 打卡失败: {detail}")
            notify("打卡失败", rows)
            return 1

        if state == "pending":
            print(f"[..] 任务已发布但还没到窗口（第 {attempt} 次检查）")
        else:
            print(f"[..] 今天还没有签到任务（第 {attempt} 次检查）")

        if time.time() > deadline:
            print(f"[!] 等待超过 {MAX_WAIT_MIN} 分钟，放弃。稍后手动重跑即可。")
            return 1
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
