# -*- coding: utf-8 -*-
"""
深流 · 每日雷达 · 云端抓取脚本
由 GitHub Actions 每天 12:00(北京时间) 定时运行，完全不依赖本机 / WorkBuddy 客户端。

架构（2026-10-27 重构）：
  1. 抓取：直接抓权威信源列表页 + 文章页，拿到**真实**的标题/链接/日期/正文片段。
  2. 整理：GLM 只基于真实素材做筛选、分类、摘要——不再让 AI "搜索"，杜绝编造。
  3. 后处理：时效过滤(宽松+兜底) → 分类限额 → 每类保底 → 写库。
  4. 任何一步拿不到数据都以非零退出码结束，避免"静默成功"。

环境变量：ZHIPU_API_KEY(必填)
"""
import os
import sys
import re
import json
import time
import html
import random
import datetime
import urllib.request
import urllib.error
import ssl
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

API_KEY = os.environ.get("ZHIPU_API_KEY", "").strip()
CHAT_MODEL = (os.environ.get("ZHIPU_MODEL") or "glm-4-flash").strip()
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

SYNC_LOAD = "https://yulin-scale-sync.onrender.com/api/load/caixie2026"
SYNC_SAVE = "https://yulin-scale-sync.onrender.com/api/save?id=caixie2026"

TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 权威信源：直接抓列表页，不依赖任何搜索 API
# 优先国内民生向（监管处罚/消费维权/品牌舆情），法规频道兜底法律法规类
SOURCES = [
    ("食品伙伴网-国内", "https://news.foodmate.net/guonei/"),
    ("中国质量新闻网-民生", "https://www.cqn.com.cn/ms/"),
    ("食品伙伴网-资讯", "https://news.foodmate.net/"),
    ("食品伙伴网-法规", "https://law.foodmate.net/"),
]

MAX_PER_SOURCE = 25   # 每个源最多取多少条候选（配额均衡，避免单源霸占）
MAX_ARTICLE_FETCH = 120  # 最多并发抓多少篇文章正文（控制耗时）
RECENT_DAYS = 10      # 时效窗口（源更新偏慢，放宽到10天再由 GLM 优先选最新）
TARGET = 20
MAX_DAILY = 30
TYPES = ["监管处罚", "法律法规", "消费骗局", "品牌舆情", "行业快讯"]

SYSTEM_PROMPT = """你是央广网民生频道记者的选题助手。下面给你一批**从权威食品行业网站真实抓取**的新闻素材（含标题、链接、发布日期、正文片段）。

你的任务只有一个：从中**筛选**出有价值的选题线索，按五大类分类，并写出摘要。

【绝对禁止】
- 禁止编造任何素材里没有的新闻、企业名、事件细节、链接。
- 禁止使用"XX公司""某品牌""某公司"这类占位符。素材里没写具体名字的，就不要收录。
- 禁止自己"补充"素材里没有的链接。链接必须原样照抄素材里给出的 url 字段。
- 如果某条素材信息不足以判断，宁可不收，也不要凑数。

【五大分类】
1. 监管处罚：抽检不合格通报、行政处罚、召回、违法广告处罚。优先总局/省级。
2. 消费骗局：虚假宣传、套用认证、成分虚标、欺诈套路、坑老会销、私域直播骗局。有深度调查价值的标 deep=true。
3. 品牌舆情：知名品牌食安事件、投诉、集体维权、媒体曝光、异物。
4. 法律法规：食品安全法/标准修订、新规征求意见、重大政策解读。
5. 行业快讯：重大行业政策、标志性关店/财报事件。

【展开合集标题 —— 重要】
素材里常有"今日导读：A；B；C"或"一周舆情汇总"这类**合集标题**，一条里包含多条新闻。
对于这类素材，请把它**拆开成多条独立线索**分别收录（每条用各自的小标题），
url 仍照抄该合集素材的 url。这是获取品牌舆情、消费骗局类线索的主要来源。

【筛选规则】
- 只要国内民生、食品安全、消费维权相关。
- **必须排除**：
  · 进出口预警/国外疫情（如出口被检出有害生物、国外H5N1禽流感、他国召回）——不是国内民生。
  · 地市级及以下的常规抽检公示（如某某市第X期抽检信息公示）——除非涉及全国知名品牌。
  · 非食品类产品质量抽查（水泥、消防器材、服装等非食品）。
  · 外交/军事/纯时政/娱乐/纯财经股市/科技数码。
- 同一事件多个来源只留 1 条。
- 优先保留日期最新的。

【数量】合计约 20 条；第 4、5 两类（法律法规、行业快讯）各自不超过 3 条；五大类每类至少 1 条。

【输出】只输出一个 JSON 数组，不要任何其他文字、不要 markdown 代码块。
每项字段：
{"title":"必须与素材标题一致，不要改写","summary":"40-80字摘要，只能基于素材正文片段概括","source":"食品伙伴网","url":"原样照抄素材里的url","type":"监管处罚|法律法规|消费骗局|品牌舆情|行业快讯","deep":true或false,"date":"素材里的发布日期YYYY-MM-DD"}
"""


def now_ts():
    return int(time.time() * 1000)


def today_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def parse_date(text):
    """从文本提取日期。"""
    if not text:
        return None
    for p in [r'(\d{4})[\-/年](\d{1,2})[\-/月](\d{1,2})', r'(\d{4})(\d{2})(\d{2})']:
        m = re.search(p, str(text))
        if m:
            try:
                return datetime.datetime(int(m.group(1)), int(m.group(2)),
                                         int(m.group(3)), tzinfo=TZ)
            except Exception:
                pass
    return None


def is_recent(dt, days=RECENT_DAYS):
    if not dt:
        return False
    delta = datetime.datetime.now(TZ) - dt
    return delta.days <= days and delta.total_seconds() >= -86400


def http_get(url, timeout=20):
    """抓取网页并按声明编码解码。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "ignore")


def clean_text(t):
    t = re.sub(r'<script.*?</script>', ' ', t, flags=re.S | re.I)
    t = re.sub(r'<style.*?</style>', ' ', t, flags=re.S | re.I)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = html.unescape(t)
    return re.sub(r'\s+', ' ', t).strip()


# 判断链接是否像"文章页"（而非首页/栏目页/静态资源）
ARTICLE_HINT = re.compile(
    r'(/\d{4}[-/]\d{1,2}|/\d{6,}\.html|/content[_-]|show-\d+|/news/[^"]*\d+\.html)', re.I)
SKIP_EXT = re.compile(r'\.(css|js|jpg|jpeg|png|gif|ico|svg|xml|json|pdf)$', re.I)


def is_article_link(link):
    if not link.startswith(("http://", "https://")):
        return False
    if SKIP_EXT.search(link):
        return False
    return bool(ARTICLE_HINT.search(link))


def fetch_list(source_url):
    """从列表页提取 (title, link)，通用适配多站点。"""
    try:
        t = http_get(source_url, timeout=25)
    except Exception as e:
        print(f"[抓取] 列表页失败 {source_url}: {type(e).__name__}")
        return []
    found = []
    # 形态1：<a href title="...">
    for link, title in re.findall(r'href="(https?://[^"]+)"[^>]*?title="([^"]{8,200})"', t):
        if is_article_link(link):
            found.append((html.unescape(title).strip(), link))
    # 形态2：<a href>纯文本</a>
    if len(found) < 5:
        for link, title in re.findall(r'href="(https?://[^"]+)"[^>]*>([^<]{10,120})', t):
            if is_article_link(link):
                found.append((html.unescape(title).strip(), link))
    out, seen = [], set()
    for title, link in found:
        if link in seen:
            continue
        seen.add(link)
        out.append((title, link))
    return out[:MAX_PER_SOURCE]


def fetch_article(job):
    """抓单篇文章：返回 (date, snippet)。日期依次尝试 URL→标题→正文。"""
    title, link = job
    date = parse_date(link) or parse_date(title)
    snippet = ""
    try:
        t = http_get(link, timeout=15)
        if not date:
            date = parse_date(t[:8000])
        snippet = clean_text(t)[:1000]
    except Exception:
        pass
    return date, snippet


def glm_chat_json(system, user, retries=2):
    payload = {"model": CHAT_MODEL,
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


def http_json(url, data=None, method=None, headers=None, timeout=300):
    h = {"User-Agent": "deep-radar/1.0", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), r.headers


def extract_json_array(text):
    start = text.find("[")
    if start == -1:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(text[start:].lstrip())
        return val if isinstance(val, list) else None
    except Exception:
        return None


def normalize(item, allowed_links):
    """整理条目；url 必须来自真实抓到的素材，否则置空。"""
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    t = str(item.get("type", "")).strip()
    if t not in TYPES:
        t = "监管处罚"
    url = str(item.get("url", "")).strip()
    if url not in allowed_links:
        url = ""  # AI 若给出素材外的链接，一律清空
    date_str = str(item.get("date", "")).strip()
    return {
        "id": f"{now_ts()}-{random.randint(1000, 9999)}",
        "title": title,
        "summary": str(item.get("summary", "")).strip(),
        "source": str(item.get("source", "")).strip() or "食品伙伴网",
        "url": url,
        "type": t,
        "deep": bool(item.get("deep", False)),
        "read": False,
        "createdAt": now_ts(),
        "eventDate": date_str,
    }


# 脚本层面硬过滤：GLM 对排除规则执行不稳定，杂音在此强制剔除
NOISE_PATTERNS = [
    r'我国出口', r'出口.{0,10}检出', r'H5N1', r'高致病性(禽流感|甲型流感)',
    r'罗马尼亚', r'爱尔兰', r'波多黎各', r'德国发生', r'韩国食药', r'欧盟',
    r'输华', r'境外', r'他国召回',
]
# 地市级/地区级常规抽检（省级用"省市场监督管理局"，总局用"市场监管总局"，不会被误杀）
LOCAL_PATTERN = r'(市市场监督管理局|市监局|地区.{0,12}(抽查|抽检|净含量))'


def is_noise(title):
    for p in NOISE_PATTERNS:
        if re.search(p, title):
            return True
    if re.search(LOCAL_PATTERN, title) and re.search(r'(抽检|抽查|公示|净含量)', title):
        return True
    return False


def dedup_regulatory(items):
    result, sigs = [], []
    for it in items:
        if it["type"] != "监管处罚":
            result.append(it)
            continue
        chars = set(re.findall(r'[\u4e00-\u9fff]', it["title"] + it["summary"]))
        dup = False
        for prev in sigs:
            inter = len(chars & prev)
            if inter >= 8 and inter / max(len(chars), len(prev)) > 0.35:
                dup = True
                break
        if not dup:
            result.append(it)
            sigs.append(chars)
    return result


def load_cloud():
    data, h = http_json(SYNC_LOAD)
    return data, h.get("X-Saved-At")


def merge_into(cloud, day, items):
    arr = cloud.setdefault("daily", {}).setdefault(day, [])
    have = {x.get("title") for x in arr}
    added = []
    for it in items:
        if it["title"] in have:
            continue
        arr.insert(0, it)
        added.append(it)
        have.add(it["title"])
    if len(arr) > MAX_DAILY:
        arr[:] = arr[:MAX_DAILY]
    return added


def save_cloud(data, base):
    resp, _ = http_json(SYNC_SAVE + "&base=" + str(base), data, method="POST")
    return resp


def main():
    if not API_KEY:
        print("ERROR: 环境变量 ZHIPU_API_KEY 未设置")
        sys.exit(1)

    today = today_str()
    print(f"== 每日雷达抓取开始 日期={today} 信源={len(SOURCES)} 整理={CHAT_MODEL} ==")

    # 1. 抓取真实素材
    jobs = []
    for name, url in SOURCES:
        lst = fetch_list(url)
        print(f"[抓取] {name}: {len(lst)} 条候选")
        jobs.extend(lst)
    if not jobs:
        print("ERROR: 所有信源均抓取失败")
        sys.exit(2)

    # 2. 并发抓文章正文/日期
    jobs = jobs[:MAX_ARTICLE_FETCH]
    print(f"[抓取] 并发抓取 {len(jobs)} 篇文章正文…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_article, jobs))

    materials, allowed_links, noise_n = [], set(), 0
    for (title, link), (date, snip) in zip(jobs, results):
        if not date or not is_recent(date):
            continue
        if is_noise(title):
            noise_n += 1
            continue
        allowed_links.add(link)
        materials.append((title, link, date.strftime("%Y-%m-%d"), snip[:500]))
    print(f"[抓取] 时效窗口内 {len(materials)} 条（剔除进出口/地市级杂音 {noise_n} 条）")
    if not materials:
        print("ERROR: 时效窗口内无真实素材")
        sys.exit(3)

    # 3. GLM 基于真实素材筛选分类
    user = "以下是真实抓取的素材（标题 | 链接 | 日期 | 正文片段）：\n\n"
    for title, link, ds, snip in materials:
        user += f"- 标题：{title}\n  链接：{link}\n  日期：{ds}\n  正文片段：{snip[:300]}\n\n"

    cleaned, seen = [], set()
    for _ in range(2):
        items = glm_chat_json(SYSTEM_PROMPT, user)
        if not items:
            continue
        for x in items:
            n = normalize(x, allowed_links)
            if n and n["title"] not in seen:
                cleaned.append(n)
                seen.add(n["title"])
        if len(cleaned) >= 18:
            break
    print(f"[整理] GLM 输出 {len(cleaned)} 条")
    if not cleaned:
        print("ERROR: GLM 未输出有效条目")
        sys.exit(4)

    cleaned = dedup_regulatory(cleaned)

    # 合集处理：GLM 有时展开合集、有时整条照抄，导致同 url 重复。
    # 同一 url 优先保留"已展开"的短标题条目，未展开的合集标题每组只留 1 条。
    def _is_collection(t):
        return ('；' in t or ';' in t) and len(t) > 45

    grouped, order = {}, []
    for it in cleaned:
        key = it.get("url", "") or it["id"]
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(it)
    merged = []
    for key in order:
        g = grouped[key]
        expanded = [x for x in g if not _is_collection(x["title"])]
        merged.extend(expanded[:4] if expanded else [g[0]])
    cleaned = merged

    # 4. 分类限额：后两类各不超过 3 条
    limited, cnt = [], {"法律法规": 0, "行业快讯": 0}
    for it in cleaned:
        t = it["type"]
        if t in cnt:
            if cnt[t] >= 3:
                continue
            cnt[t] += 1
        limited.append(it)
    cleaned = limited

    # 5. 五大类保底
    have = {t: [] for t in TYPES}
    for it in cleaned:
        have[it["type"]].append(it)
    ensured = [have[t][0] for t in TYPES if have[t]]
    eids = {x["id"] for x in ensured}
    cleaned = ensured + [x for x in cleaned if x["id"] not in eids]
    if len(cleaned) > TARGET:
        cleaned = cleaned[:TARGET]

    with_url = sum(1 for x in cleaned if x.get("url"))
    print(f"[整理] 最终 {len(cleaned)} 条（带真实链接 {with_url} 条）")
    if not cleaned:
        print("ERROR: 无有效条目")
        sys.exit(5)

    # 6. 写库
    data, base = load_cloud()
    for attempt in range(4):
        added = merge_into(data, today, cleaned)
        try:
            save_cloud(data, base)
            print(f"[写库] 成功写入 {len(added)} 条到 {today}")
            from collections import Counter
            print("[写库] 分类分布:", dict(Counter(x["type"] for x in added)))
            break
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 3:
                time.sleep(2)
                data, base = load_cloud()
                continue
            print(f"ERROR: 写库失败 {e.code}")
            sys.exit(6)
    else:
        print("ERROR: 写库多次冲突")
        sys.exit(6)

    print("== 抓取完成 ==")


if __name__ == "__main__":
    main()
