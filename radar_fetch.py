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
3. 品牌舆情：知名品牌（含连锁餐饮/零售/食品企业）的食安事件、投诉、集体维权、媒体曝光、异物、经营争议。
   素材里出现**具体品牌名**（如宜家、霸王茶姬、三只松鼠、山姆等）的消费类争议，一律归到本类。
   **本类每天必须至少 1 条**，只要素材里有品牌名相关的内容就必须选进来。
4. 法律法规：食品安全法/标准修订、新规征求意见、重大政策解读。
5. 行业快讯：重大行业政策、标志性关店/财报事件。

【展开合集标题 —— 必做，最重要的一步】
素材里常见两类"合集"，**必须拆开成多条独立线索分别收录，绝不允许整条当作一个选题输出**：
A. 「今日导读：A；B；C（2026年X月X日）」—— 按分号拆开，每一条都单独收录。
B. 「食品行业一周舆情信息汇总（10.26-11.01）」「食品行业产经动态：…」—— 从正文片段里提炼出其中的具体事件，逐条收录。
要求：
- 每条用**各自的小标题**做 title（不要保留"今日导读："前缀，不要保留日期括号）。
- url 一律照抄该合集素材给出的 url；date 用该合集素材的日期。
- deep=true 的判断按每条内容单独判断。
这是获取"品牌舆情""消费骗局"两类线索的**主要来源**，请务必充分展开，不要嫌条数多。

【筛选规则】
- 只要国内民生、食品安全、消费维权相关。
- **必须排除**：
  · 进出口预警/国外疫情（如出口被检出有害生物、国外H5N1禽流感、他国召回）——不是国内民生。
  · 地市级及以下的常规抽检公示（如某某市第X期抽检信息公示）——除非涉及全国知名品牌。
  · 非食品类产品质量抽查（水泥、消防器材、服装等非食品）。
  · 外交/军事/纯时政/娱乐/纯财经股市/科技数码。
- 同一事件多个来源只留 1 条。
- 优先保留日期最新的。

【数量 —— 重要】
给你的素材**已经由脚本预先剔除**了境外/进出口预警、地市级常规抽检、财报资本类、非食品类杂音，
剩下的基本都属于可用的国内民生线索。因此请**尽量把素材全部用上**：除非明显重复或明显与食品消费无关，
不要自己再额外筛掉。目标 15~20 条；第 4、5 两类（法律法规、行业快讯）各自不超过 3 条；五大类每类至少 1 条。

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
        snippet = clean_text(t)[:2000]
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
# ------------------------------------------------------------------
# A. 境外国家/地区：涉食品动作词才算（避免误杀"进口酱油造假"这类国内打假稿）
FOREIGN_COUNTRIES = (
    r'日本|韩国|朝鲜|美国|欧盟|英国|法国|德国|意大利|西班牙|葡萄牙|荷兰|比利时|'
    r'奥地利|瑞士|瑞典|挪威|丹麦|芬兰|冰岛|爱尔兰|波兰|捷克|匈牙利|罗马尼亚|'
    r'保加利亚|希腊|克罗地亚|塞尔维亚|乌克兰|白俄罗斯|俄罗斯|土耳其|塞浦路斯|'
    r'加拿大|墨西哥|巴西|阿根廷|智利|秘鲁|哥伦比亚|厄瓜多尔|乌拉圭|巴拉圭|'
    r'澳大利亚|新西兰|越南|泰国|老挝|柬埔寨|缅甸|马来西亚|新加坡|印度尼西亚|'
    r'印尼|菲律宾|文莱|印度|巴基斯坦|孟加拉|斯里兰卡|尼泊尔|蒙古|哈萨克斯坦|'
    r'乌兹别克斯坦|伊朗|伊拉克|沙特|阿联酋|以色列|埃及|南非|尼日利亚|肯尼亚|'
    r'摩洛哥|突尼斯|埃塞俄比亚|加纳'
)
FOREIGN_ACTION = (
    r'召回|检出|通报|命令检查|监控检查|禁止进口|暂停进口|禁止输华|'
    r'疫情|感染|暴发|爆发|销毁|退运|拒绝入境|不合格'
)
# B. 疫情/疫病（无论哪个国家，与国内民生消费无关）
PLAGUE_PATTERNS = [
    r'H5N1', r'H7N9', r'H9N2', r'高致病性', r'禽流感', r'非洲猪瘟', r'口蹄疫',
    r'沙门氏菌感染疫情', r'李斯特菌感染疫情', r'大肠杆菌疫情',
    r'(发生|爆发|暴发).{0,8}疫情',
]
# C. 进出口预警（针对我国出口被检出/被拦）
EXPORT_PATTERNS = [
    r'我国出口', r'中国产.{0,15}(命令检查|监控检查|拒绝入境|退运)',
    r'出口.{0,12}(检出|受阻|退回|被拒)', r'输华', r'退运', r'拒绝入境',
    r'(已)?销至我国', r'进口.{0,20}(检出|不合格|涉疫|阳性)',
]
# D. 财报/资本市场（用户明确不要普通財报、关店、上市、人事）
FINANCE_PATTERNS = [
    r'(营收|营业收入|净利|净利润|毛利|利润总额).{0,18}(亿|万元|元|同比|增长|下降|微降)',
    r'(前三季度|上半年|一季度|三季度|全年|半年报|年报|季报|财报|业绩快报|业绩预告)',
    r'(拟|宣布|完成|筹划).{0,10}(易主|收购|并购|重组|股权转让|增持|减持|私有化)',
    r'(涨停|跌停|股价|市值|定增|融资|IPO|上市申请|过会|挂牌)',
    r'(经销商峰会|新品发布|招商会|品牌战略发布会|签约代言)',
]
# E. 非食品品类（产品质量抽查里的建材/电器/服装等）
NONFOOD_PATTERNS = [
    r'水泥|钢筋|电线电缆|消防器材|燃气灶|电热水器|儿童玩具|学生文具|'
    r'成品油|化肥|农膜|电动自行车|头盔|安全帽|电子计价秤|计量器具',
]

# 地市级/地区级常规抽检（省级用"省市场监督管理局"，总局用"市场监管总局"，不会被误杀）
LOCAL_PATTERN = r'(市市场监督管理局|市监局|地区.{0,12}(抽查|抽检|净含量))'
FOREIGN_PATTERN = f'(?:{FOREIGN_COUNTRIES}).{{0,30}}(?:{FOREIGN_ACTION})'

# 噪声按"严重程度"分级：素材太少时，可从最轻一级开始回补，避免空窗
NOISE_LEVELS = [
    ("境外", [FOREIGN_PATTERN]),          # 最硬：外国的事，与国内民生无关
    ("疫情", PLAGUE_PATTERNS),
    ("出口", EXPORT_PATTERNS),
    ("财报", FINANCE_PATTERNS),
    ("非食品", NONFOOD_PATTERNS),
]
# 可从最轻一级回补的顺序（地市级最轻，境外/疫情永不回补）
RECOVERABLE = ["地市级", "非食品", "财报", "出口"]


def is_noise(title):
    """返回 (是否噪声, 原因类别)。"""
    if re.search(LOCAL_PATTERN, title) and re.search(r'(抽检|抽查|公示|净含量|新规)', title):
        return True, "地市级"
    for label, pats in NOISE_LEVELS:
        for p in pats:
            if re.search(p, title):
                return True, label
    return False, ""


def expand_collection(title, link, ds, snip):
    """把「今日导读：A；B；C」在脚本层拆成独立素材。
    GLM 展开合集时好时坏（一次 18 条一次 9 条），索性由脚本拆好，AI 只做分类。"""
    m = re.match(r'^\s*(?:今日导读|今日要闻|导读|今日关注)[：:]\s*(.+)$', title)
    if not m:
        return [(title, link, ds, snip)]
    body = re.sub(r'[（(]\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*[)）]\s*$', '', m.group(1))
    body = re.sub(r'[（(]\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*[)）]\s*$', '', body)
    parts = [p.strip(' 。.…') for p in re.split(r'[；;]', body)]
    parts = [p for p in parts if len(p) >= 8]
    if len(parts) < 2:
        return [(title, link, ds, snip)]
    return [(p, link, ds, snip) for p in parts]


def dedup_titles(items):
    """标题近似去重：同一稿件的不同标题（一个带副标题、一个不带）算重复。
    判定：短标题是长标题的子串，或汉字集合 Jaccard 相似度 > 0.72。"""
    out = []
    for it in items:
        t = it["title"].strip()
        dup = False
        for prev in out:
            p = prev["title"].strip()
            if not t or not p:
                continue
            if len(t) >= 10 and len(p) >= 10 and (t in p or p in t):
                dup = True
                break
            ca = set(re.findall(r'[\u4e00-\u9fff]', t))
            cb = set(re.findall(r'[\u4e00-\u9fff]', p))
            if ca and cb:
                inter = len(ca & cb)
                if inter >= 10 and inter / max(len(ca), len(cb)) > 0.85:
                    dup = True
                    break
        if not dup:
            out.append(it)
    return out


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

    # 0. 幂等保护：定时任务设了多个时间点互为备份，当天已有足量数据就不再重复抓
    if not os.environ.get("FORCE_RUN"):
        try:
            _d, _h = load_cloud()
            _n = len(_d.get("daily", {}).get(today, []))
            if _n >= 8:
                print(f"[跳过] 今天 {today} 已有 {_n} 条数据，本次定时运行不重复抓取")
                sys.exit(0)
        except Exception as e:
            print(f"[跳过检查] 云端读取失败，继续抓取：{type(e).__name__}")

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

    window, dropped = [], []
    seen_key = set()
    for (title, link), (date, snip) in zip(jobs, results):
        if not date or not is_recent(date):
            continue
        # 素材层去重：同一标题或同一链接只留一份（不同源常互相转载）
        key = (title.strip(), link.strip())
        if title.strip() in seen_key or link.strip() in seen_key:
            continue
        bad, why = is_noise(title)
        rec = (title, link, date.strftime("%Y-%m-%d"), snip[:1500], why)
        if bad:
            dropped.append(rec)
        else:
            window.append(rec)
            seen_key.add(title.strip())
            seen_key.add(link.strip())

    # 兜底：干净素材太少时，从"最轻"的噪声逐级回补，绝不空窗（境外/疫情永不回补）
    MIN_MATERIALS = 8
    recovered = []
    if len(window) < MIN_MATERIALS:
        for level in RECOVERABLE:
            if len(window) >= MIN_MATERIALS:
                break
            for rec in list(dropped):
                if len(window) >= MIN_MATERIALS:
                    break
                if rec[4] == level:
                    window.append(rec)
                    dropped.remove(rec)
                    recovered.append(level)

    # 合集预拆：「今日导读：A；B；C」拆成独立素材，减轻 GLM 负担、稳定每日条数
    expanded = []
    for t, l, d, s, _ in window:
        expanded.extend(expand_collection(t, l, d, s))
    if len(expanded) > len(window):
        print(f"[抓取] 合集预拆：{len(window)} → {len(expanded)} 条素材（+{len(expanded)-len(window)}）")
    materials = expanded
    allowed_links = {m[1] for m in materials}
    tally = {}
    for rec in dropped:
        tally[rec[4]] = tally.get(rec[4], 0) + 1
    print(f"[抓取] 时效窗口内可用素材 {len(materials)} 条；剔除噪声 {dict(tally)}"
          + (f"；回补 {recovered}" if recovered else ""))
    if not materials:
        print("ERROR: 时效窗口内无真实素材")
        sys.exit(3)

    # 3. GLM 基于真实素材筛选分类
    COLLECTION_HINT = re.compile(r'导读|汇总|盘点|一览|速览|产经动态|周报|日报')
    user = "以下是真实抓取的素材（标题 | 链接 | 日期 | 正文片段）：\n\n"
    for title, link, ds, snip in materials:
        # 合集类素材给更长正文，便于把里面的子条目完整拆出来
        body = snip[:1200] if COLLECTION_HINT.search(title) else snip[:320]
        user += f"- 标题：{title}\n  链接：{link}\n  日期：{ds}\n  正文片段：{body}\n\n"

    cleaned, seen = [], set()
    for rnd in range(3):
        # 第二轮起带上"已收录清单"，逼模型补遗漏，避免它每轮重复吐同样的几条
        prompt = user
        if seen:
            prompt += ("\n\n【补充轮次要求】上一轮已收录以下条目：\n"
                       + "\n".join(f"- {t}" for t in list(seen)[:15])
                       + "\n请**只补充上面没提到的**素材，把素材池里还没被收录的线索补全。"
                         "不要重复输出已收录的条目。若确实已全部覆盖，返回空数组 []。")
        items = glm_chat_json(SYSTEM_PROMPT, prompt)
        if not items:
            break
        added = 0
        for x in items:
            n = normalize(x, allowed_links)
            if n and n["title"] not in seen:
                cleaned.append(n)
                seen.add(n["title"])
                added += 1
        print(f"[整理] 第{rnd+1}轮：新增 {added} 条，累计 {len(cleaned)}")
        if added == 0:
            break
    print(f"[整理] GLM 输出 {len(cleaned)} 条")
    if not cleaned:
        print("ERROR: GLM 未输出有效条目")
        sys.exit(4)

    cleaned = dedup_regulatory(cleaned)

    # 3.5 二次噪声过滤：GLM 把合集拆出的子条目是"新造"的标题，素材层过滤没管到，
    #     这里必须再筛一遍（财报/境外/疫情类会从合集里漏进来）。全删光则保留原样，绝不空窗。
    refined, ref_drop = [], []
    for it in cleaned:
        bad, why = is_noise(it["title"])
        if bad:
            ref_drop.append((why, it["title"]))
            continue
        refined.append(it)
    if ref_drop:
        print(f"[过滤] 展开后二次剔除 {len(ref_drop)} 条：" +
              "; ".join(f"{w}:{t[:18]}" for w, t in ref_drop[:6]))
    if refined:
        cleaned = refined

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

    # 标题近似去重：同一稿件不同标题（带/不带副标题）统一合并
    before_n = len(cleaned)
    cleaned = dedup_titles(cleaned)
    if len(cleaned) < before_n:
        print(f"[整理] 标题近似去重 {before_n} → {len(cleaned)} 条")

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
