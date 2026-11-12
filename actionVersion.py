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

配置来源（优先级从高到低）：
  1. 环境变量（GitHub Actions Secrets）
  2. 同目录 config.local.json（本地测试用，别提交到仓库）
"""
import base64
import json
import os
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

# 签到窗口（北京时间，仅用于展示和默认值；真正的窗口以接口返回的 start/end 为准）
WINDOW_START = os.environ.get("window_start", "22:30")
WINDOW_END = os.environ.get("window_end", "23:00")
# 最多等多久（分钟）
MAX_WAIT_MIN = int(os.environ.get("max_wait_min", "20"))


def env(name, default=""):
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    # 回落到本地配置文件
    try:
        with open(os.path.join(HERE, "config.local.json"), "r", encoding="utf-8") as f:
            local = json.load(f)
        if name in local and str(local[name]).strip() != "":
            return str(local[name]).strip()
    except Exception:
        pass
    return default


def msg_send(title, info):
    mail_address = env("mail_address")
    if mail_address:
        try:
            import yagmail
            yagmail.SMTP(mail_address, env("mail_password"), env("mail_host")).send(
                env("receive_mail"), title, info)
            print("[推送] 邮件已发送")
        except Exception as e:
            print("[推送] 邮件失败:", e)
    sct = env("sct_ftqq")
    if sct:
        try:
            requests.get(f"https://sctapi.ftqq.com/{sct}.send?"
                         + urllib.parse.urlencode({"title": title, "desp": info}), timeout=15)
            print("[推送] Server酱已发送")
        except Exception as e:
            print("[推送] Server酱失败:", e)


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
        raise SystemExit(f"[X] 登录失败: {body}\n"
                         f"    账号: {username}\n"
                         f"    密码错 5 次会冻结，别连续重跑。")
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
        return []
    return body.get("data") or []


def fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000)) if ms else "-"


def find_task(logs, username):
    """返回今天已签到 / 待签到 / 无任务 三种情况。"""
    now_ms = int(time.time() * 1000)
    today = time.strftime("%Y-%m-%d")
    for it in logs:
        start, end = int(it.get("start") or 0), int(it.get("end") or 0)
        if time.strftime("%Y-%m-%d", time.localtime(start / 1000)) != today:
            continue
        print(f"[..] 今日任务: {it.get('signTitle')}  窗口 {fmt(start)} -> {fmt(end)}  "
              f"signStatus={it.get('signStatus')}")
        signed = str(it.get("signStatus")) in ("2", "3")
        if signed:
            return ("signed", it)
        if now_ms < start:
            return ("pending", it)
        if now_ms <= end:
            return ("open", it)
        return ("expired", it)
    return ("none", None)


def do_blue_sign(session, task):
    dev = (task.get("deviceList") or [{}])[0]
    uuid = dev.get("uuid") or ""
    major = dev.get("major") or ""
    if not uuid:
        raise SystemExit("[X] 任务里没有 deviceList，无法蓝牙打卡")
    blue1 = [uuid.replace("-", "") + str(major)]
    blue2 = [uuid]
    payload = {"blue1": blue1, "blue2": blue2}
    params = {"id": task.get("locationId"), "signId": task.get("signId")}
    print(f"[..] 提交蓝牙打卡: {json.dumps(payload)}")
    r = session.post(f"{BASE}/dormSign/mobile/receive/doSignByDevice",
                     headers=api_headers(), params=params,
                     data=json.dumps(payload), timeout=25)
    print(f"[..] 返回: {r.text[:400]}")
    try:
        ok = r.json().get("code") == 0
    except Exception:
        ok = False
    return ok, r.text[:300]


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
    while True:
        attempt += 1
        logs = get_sign_logs(session)
        state, task = find_task(logs, username)

        if state == "signed":
            print(f"[OK] 今天已经签到过了（{task.get('signTitle')}），无需重复")
            return 0
        if state == "expired":
            print("[!] 今天的窗口已过且未签到")
            msg_send("签到失败", f"{username} 窗口已过，未签到")
            return 1
        if state == "open":
            ok, detail = do_blue_sign(session, task)
            if ok:
                print(f"[OK] {username} 蓝牙打卡成功")
                msg_send("打卡成功", f"{username} 蓝牙打卡成功")
                return 0
            print(f"[X] 打卡失败: {detail}")
            msg_send("打卡失败", f"{username}: {detail}")
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
