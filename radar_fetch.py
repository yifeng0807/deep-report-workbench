# -*- coding: utf-8 -*-
"""
深流 · 每日雷达 · 云端抓取脚本
由 GitHub Actions 每天 12:00(北京时间) 定时运行，完全不依赖本机 / WorkBuddy 客户端。

流程：GLM 联网搜索(五大分类) -> GLM 整理为结构化 JSON -> 写入云端同步服务。
环境变量：ZHIPU_API_KEY(必填)、ZHIPU_MODEL(可选，默认 glm-4-flash)
"""
import os
import sys
import json
import time
import random
import datetime
import urllib.request
import urllib.error

API_KEY = os.environ.get("ZHIPU_API_KEY", "").strip()
MODEL = (os.environ.get("ZHIPU_MODEL") or "glm-4-flash").strip()
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

SYNC_LOAD = "https://yulin-scale-sync.onrender.com/api/load/caixie2026"
SYNC_SAVE = "https://yulin-scale-sync.onrender.com/api/save?id=caixie2026"

TZ = datetime.timezone(datetime.timedelta(hours=8))

# 五大分类及其搜索关键词（运行时拼接"近3天"日期范围）
CATEGORIES = [
    ("监管处罚", "市场监管总局 食品 抽检 不合格 通报 行政处罚 产品召回"),
    ("法律法规", "食品安全法 食品标准 修订 征求意见 市场监管总局 卫健委 药监局"),
    ("消费骗局", "食品 虚假宣传 欺诈 骗局 会销 坑老 私域直播 养生 查处"),
    ("品牌舆情", "食品品牌 食品安全 投诉 曝光 异物 黑猫投诉 热搜"),
    ("行业快讯", "食品饮料 企业 财报 关店 乳制品 餐饮 行业动态"),
]

MAX_DAILY = 30  # 每天最多条目数（硬性上限）

# 整理阶段的系统提示（复用自动化筛选规则）
SYSTEM_PROMPT = """你是央广网民生频道记者的选题助手，负责从"食品消费"领域的搜索结果中筛选整理出有价值的选题线索。

【分类与数量】重点抓前三类，后两类只挑最关键，合计 15~30 条，严格不超过 30 条：
1. 监管处罚/抽检通报（重点，6-10 条）：市监总局及各地市监局食品抽检不合格、行政处罚、召回、违法广告处罚；优先总局与省级发布。
2. 消费骗局/欺骗手法（重点，4-8 条）：虚假宣传、套用认证、成分纯度虚标、欺诈套路、坑老会销等——最有深度调查价值，尽量抓全并标 deep=true。
3. 大品牌投诉与舆情（重点，4-8 条）：知名品牌食安事件、投诉、集体维权、媒体曝光、热搜。
4. 法律法规（0-2 条）：只挑最关键——食品安全法修订、重要食品标准出台/征求意见、重大政策解读等，普通动态不收。
5. 行业快讯（0-2 条）：只挑最关键——重大行业政策、标志性关店/财报事件等，普通财报关店上市人事不收。
注意：第 4、5 两类（法律法规+行业快讯）合计不超过 3 条。

【筛选铁律】只要：民生、食品安全、消费维权相关。
排除：外交/国际/军事/纯时政/广告软文/娱乐八卦/纯财经股市/科技数码/泛商业公司股权人事变更/影视娱乐互联网动态——除非直接涉及食品消费或消费者权益。
- 时效：只收近 3 天内容，优先最新；7 天以前的坚决不选。
- 地域门槛：地市级及以下（地级市/区县/县级市/单个小门店）的常规抽检通报、个案处罚、本地典型案例一般不选，除非涉及全国知名品牌、影响面极广或事情闹得特别大（如梧州、贺州、惠州、南通通州这类直接排除）。
- 同一事件多个来源合并为一条，不要拆成重复多条。
- 信源白名单优先：红星新闻、极目新闻、澎湃新闻（尤其"马上测"栏目）、人民日报、央广网、中国新闻周刊、界面新闻、新京报、封面新闻、中国青年报、潇湘晨报、南方都市报、北京日报、新华社、中国食品报、庶正康讯、中新经纬、国是直通车、央视新闻、北京青年报、中国质量新闻网、中国消费者报 等，以及市场监管总局、卫健委、药监局、中消协官方发布。

【输出格式】只输出一个 JSON 数组，不要任何其他文字、不要 markdown 代码块、不要解释。
数组每项字段（全部字符串或布尔）：
{"title":"一句话标题","summary":"30-60字摘要","source":"来源机构或媒体名","url":"原文链接(必须是搜索结果里真实出现的链接，没有就留空字符串，禁止编造)","type":"监管处罚|法律法规|消费骗局|品牌舆情|行业快讯 之一","deep":true或false}

deep=true 表示有深度调查操作空间（虚假宣传、欺诈套路、系统性食安问题、可复制的坑老套路等）。"""


def now_ts():
    return int(time.time() * 1000)


def today_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def date_range_str():
    end = datetime.datetime.now(TZ)
    start = end - datetime.timedelta(days=3)
    return f"{start.strftime('%Y年%m月%d日')}至{end.strftime('%Y年%m月%d日')}"


def http_json(url, data=None, method=None, headers=None, timeout=120):
    h = {"User-Agent": "deep-radar/1.0", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), r.headers


def glm_search(query):
    """调用 GLM 的 web_search 工具联网搜索，返回模型整理出的文本。"""
    messages = [{"role": "user", "content": "请联网搜索以下主题的最新新闻，逐条列出：标题、来源、日期、一句话要点。\n\n主题：" + query}]
    tools = [{"type": "web_search", "web_search": {"search_query": query}}]
    for _ in range(3):
        payload = {"model": MODEL, "messages": messages, "tools": tools, "temperature": 0.3}
        data, _ = http_json(GLM_URL, payload, headers={"Authorization": f"Bearer {API_KEY}"})
        msg = data["choices"][0]["message"]
        if msg.get("content"):
            return msg["content"].strip()
        tc = msg.get("tool_calls")
        if not tc:
            return ""
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tc})
        for c in tc:
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps({"search_query": query}, ensure_ascii=False)})
    return ""


def glm_chat_json(system, user, retries=2):
    """纯文本整理，要求模型输出严格 JSON 数组。"""
    payload = {"model": MODEL,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": 0.2}
    for _ in range(retries + 1):
        data, _ = http_json(GLM_URL, payload, headers={"Authorization": f"Bearer {API_KEY}"})
        content = data["choices"][0]["message"]["content"] or ""
        arr = extract_json_array(content)
        if arr is not None:
            return arr
        time.sleep(2)
    return None


def extract_json_array(text):
    start = text.find("[")
    if start == -1:
        return None
    dec = json.JSONDecoder()
    try:
        val, _ = dec.raw_decode(text[start:].lstrip())
        if isinstance(val, list):
            return val
    except Exception:
        pass
    return None


def normalize(item):
    types = {"监管处罚", "法律法规", "消费骗局", "品牌舆情", "行业快讯"}
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    t = str(item.get("type", "")).strip()
    if t not in types:
        t = "监管处罚"
    return {
        "id": f"{now_ts()}-{random.randint(1000, 9999)}",
        "title": title,
        "summary": str(item.get("summary", "")).strip(),
        "source": str(item.get("source", "")).strip(),
        "url": str(item.get("url", "")).strip(),
        "type": t,
        "deep": bool(item.get("deep", False)),
        "read": False,
        "createdAt": now_ts(),
    }


def load_cloud():
    data, h = http_json(SYNC_LOAD)
    return data, h.get("X-Saved-At")


def merge_into(cloud, day, items):
    arr = cloud.setdefault("daily", {}).setdefault(day, [])
    have_titles = {x.get("title") for x in arr}
    have_urls = {x.get("url") for x in arr if x.get("url")}
    added = []
    for it in items:
        if it["title"] in have_titles:
            continue
        if it["url"] and it["url"] in have_urls:
            continue
        arr.insert(0, it)
        added.append(it)
        have_titles.add(it["title"])
        if it["url"]:
            have_urls.add(it["url"])
    if len(arr) > MAX_DAILY:
        arr[:] = arr[:MAX_DAILY]
    return added


def save_cloud(data, base):
    url = SYNC_SAVE + "&base=" + str(base)
    resp, h = http_json(url, data, method="POST")
    return resp


def main():
    if not API_KEY:
        print("ERROR: 环境变量 ZHIPU_API_KEY 未设置")
        sys.exit(1)

    today = today_str()
    dr = date_range_str()
    print(f"== 每日雷达抓取开始 日期={today} 范围={dr} 模型={MODEL} ==")

    # 1. 五大分类搜索
    gathered = {}
    for cat, kw in CATEGORIES:
        q = f"近3天（{dr}）{kw}"
        try:
            txt = glm_search(q)
        except Exception as e:
            txt = ""
            print(f"[搜索] {cat} 异常: {e}")
        gathered[cat] = txt
        print(f"[搜索] {cat} 完成，返回 {len(txt)} 字")

    # 2. 整理为结构化 JSON
    user = "以下是今天抓到的食品消费领域线索（按分类）。请按规则筛选整理并输出 JSON 数组。\n\n"
    for cat, txt in gathered.items():
        user += f"\n=====【{cat}】=====\n{txt[:6000] if txt else '（无结果）'}\n"

    items = glm_chat_json(SYSTEM_PROMPT, user)
    if items is None:
        print("ERROR: 整理阶段未能产出有效 JSON")
        sys.exit(2)

    cleaned = [n for n in (normalize(x) for x in items) if n]
    if len(cleaned) > MAX_DAILY:
        cleaned = cleaned[:MAX_DAILY]
    print(f"[整理] 模型输出 {len(items)} 条，清洗后有效 {len(cleaned)} 条")

    if not cleaned:
        print("WARN: 无有效条目，跳过写库")
        sys.exit(0)

    # 3. 写库（带版本号，409 重试）
    data, base = load_cloud()
    for attempt in range(4):
        added = merge_into(data, today, cleaned)
        try:
            save_cloud(data, base)
            print(f"[写库] 成功写入 {len(added)} 条到 {today}")
            from collections import Counter
            print("[写库] 分类分布:", dict(Counter(x['type'] for x in added)))
            break
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 3:
                time.sleep(2)
                data, base = load_cloud()
                continue
            print(f"ERROR: 写库失败 {e.code} {e.read().decode('utf-8', 'ignore')[:200]}")
            sys.exit(3)
    else:
        print("ERROR: 写库多次冲突仍失败")
        sys.exit(3)

    print("== 抓取完成 ==")


if __name__ == "__main__":
    main()
