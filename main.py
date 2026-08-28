"""
신문 사설 자동 요약 & 이메일 발송 봇 v2026.06.23-1303
구조: 신문사/제목/작성자 → AI 요약 → 원문 (신문사별 개별 구성)
"""

import os, re, smtplib, json, hashlib, shutil, time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

# Gemini 2.5는 신규 API 키에서 404 — latest alias / 3.5 계열 사용
GEMINI_MODELS = [
    ("v1beta", "gemini-flash-latest"),
    ("v1beta", "gemini-flash-lite-latest"),
    ("v1beta", "gemini-3.5-flash"),
]
GROQ_MODELS = [
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
]
GROQ_COOLDOWN_SEC = 8


def _retry_after_seconds(resp):
    msg = _ai_error_detail(resp)
    match = re.search(r"try again in ([\d.]+)s", msg, re.I)
    return float(match.group(1)) + 1 if match else GROQ_COOLDOWN_SEC


def _groq_request_body(model, prompt, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["max_completion_tokens"] = max_tokens
    elif "qwen" in model:
        body["reasoning_effort"] = "none"
        body["max_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
    return body


def _groq_extract_text(data):
    msg = data.get("choices", [{}])[0].get("message", {})
    content = (msg.get("content") or "").strip()
    if content:
        return content
    for key in ("reasoning", "reasoning_content"):
        val = (msg.get(key) or "").strip()
        if val:
            return val
    return ""


def _gemini_credits_depleted(resp):
    return "prepayment credits" in _ai_error_detail(resp).lower()


def _ai_error_detail(resp):
    try:
        data = resp.json()
        err = data.get("error", data)
        if isinstance(err, dict):
            return err.get("message", str(err)[:200])
        return str(data)[:200]
    except Exception:
        return resp.text[:200]


def _parse_ai_json(text):
    text = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"[\[{][\s\S]*[\]}]", text)
        if match:
            return json.loads(match.group())
        raise


def call_gemini(prompt, gemini_key, timeout=60):
    if not gemini_key:
        return None
    for ver, model in GEMINI_MODELS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={gemini_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_ai_json(text)
            if r.status_code == 429 and _gemini_credits_depleted(r):
                print("    ⚠️ Gemini: 무료 티어인데 '선불 크레딧 소진' 오류 — 실제 사용량 문제가 아니라 Google 프로젝트 설정/동기화 버그일 수 있음")
                print("       → AI Studio(https://ai.studio)에서 새 프로젝트 + API 키 생성 후 GEMINI_API_KEY 교체")
                return None
            print(f"    ⚠️ Gemini/{model}: HTTP {r.status_code} — {_ai_error_detail(r)}")
        except Exception as e:
            print(f"    ⚠️ Gemini/{model}: {e}")
    return None


def call_groq(prompt, groq_key, max_tokens=3000, timeout=60, retries=3):
    if not groq_key:
        return None
    for model in GROQ_MODELS:
        for attempt in range(retries):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                    json=_groq_request_body(model, prompt, max_tokens),
                    timeout=timeout,
                )
                if r.status_code == 200:
                    text = _groq_extract_text(r.json())
                    if not text:
                        print(f"    ⚠️ Groq/{model}: empty response (reasoning consumed token budget?)")
                        break
                    return _parse_ai_json(text)
                if r.status_code == 429 and attempt < retries - 1:
                    wait = _retry_after_seconds(r)
                    print(f"    ⏳ Groq/{model}: rate limit, {wait:.0f}s 후 재시도 ({attempt + 2}/{retries})...")
                    time.sleep(wait)
                    continue
                print(f"    ⚠️ Groq/{model}: HTTP {r.status_code} — {_ai_error_detail(r)}")
                break
            except Exception as e:
                print(f"    ⚠️ Groq/{model}: {e}")
                break
    return None


def call_ai(prompt, gemini_key, groq_key, max_tokens=3000, timeout=60, prefer="groq"):
    """AI 호출. prefer='gemini'면 요약용(빠른 TPM), 'groq'면 선별용."""
    order = ("gemini", "groq") if prefer == "gemini" else ("groq", "gemini")
    for name in order:
        if name == "gemini":
            result = call_gemini(prompt, gemini_key, timeout=timeout)
            if result:
                return result, "Gemini"
        else:
            result = call_groq(prompt, groq_key, max_tokens=max_tokens, timeout=timeout)
            if result:
                return result, "Groq"
    return None, None


def _groq_cooldown(provider):
    """Groq TPM 한도 회복 — Groq 사용 시에만 대기."""
    if provider == "Groq":
        time.sleep(GROQ_COOLDOWN_SEC)


def get_time_window():
    now = datetime.now(KST)
    if now.hour < 12:
        start = (now - timedelta(days=1)).replace(hour=18, minute=50, second=0, microsecond=0)
        end   = now.replace(hour=6, minute=59, second=59, microsecond=0)
        edition = "🌅 아침판"
    else:
        start = now.replace(hour=7, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=18, minute=50, second=59, microsecond=0)
        edition = "🌆 저녁판"
    return start, end, edition

PAPERS = ["한겨레", "조선일보", "동아일보", "경향신문", "중앙일보"]

PAPER_DOMAINS = {
    "한겨레":   ["hani.co.kr"],
    "조선일보": ["chosun.com"],
    "동아일보": ["donga.com"],
    "경향신문": ["khan.co.kr"],
    "중앙일보": ["joongang.co.kr", "joins.com"],
}

DOMAIN_TO_PAPER = {
    "hani.co.kr":     "한겨레",
    "chosun.com":     "조선일보",
    "donga.com":      "동아일보",
    "khan.co.kr":     "경향신문",
    "joongang.co.kr": "중앙일보",
    "joins.com":      "중앙일보",
    "yna.co.kr":      "연합뉴스",
    "yonhapnews.co.kr": "연합뉴스",
    "hankyung.com":   "한국경제",
    "mk.co.kr":       "매일경제",
    "mt.co.kr":       "머니투데이",
    "sedaily.com":    "서울경제",
    "ytn.co.kr":      "YTN",
    "sbs.co.kr":      "SBS",
    "mbc.co.kr":      "MBC",
    "kbs.co.kr":      "KBS",
    "jtbc.co.kr":     "JTBC",
    "sisain.co.kr":   "시사인",
}

BLOCKED_DOMAINS = ["nongaek.com", "newsis.com", "news1.kr"]

HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
}

PAPER_CONFIG = {
    "한겨레":   {"body": [".article-text", ".text"], "author": [".byline strong"]},
    "조선일보": {"body": [".article-body"],           "author": [".article__author-name"]},
    "동아일보": {"body": [".article_txt"],            "author": [".reporter_name"]},
    "경향신문": {"body": [".art_body"],               "author": [".reporter_area .name"]},
    "중앙일보": {"body": [".article_body"],           "author": [".byline__name"]},
    "연합뉴스": {"body": ["#articleWrap", ".story-news", "#article-view-content-div", ".article"],
                "author": [".writer-info", ".txt-info", ".byline"]},
    "한국경제": {"body": ["#articletxt", ".article-body", "#article-body-content"],
                "author": [".author-info", ".byline"]},
    "매일경제": {"body": [".art_txt", "#article_body", ".news_cnt_detail_wrap"],
                "author": [".author_name", ".byline"]},
    "머니투데이": {"body": ["#textBody", ".view_text", ".article_body"],
                 "author": [".name", ".byline"]},
    "시사인":   {"body": [".article-body", "#article-view-content-div", ".view-content"],
                "author": [".writer", ".byline"]},
}
DEFAULT_BODY   = [
    "article", ".article", ".news_body", "#articleBody", "#article-view-content-div",
    ".article_body", ".article-body", ".news_cnt_detail_wrap", "main", ".content",
]
DEFAULT_AUTHOR = [".author", ".byline", ".reporter", ".writer"]
UI_NOISE = ["공유하기", "카카오톡으로 공유하기", "URL 복사", "창 닫기", "SNS"]


def detect_paper(url):
    return next((p for d, p in DOMAIN_TO_PAPER.items() if d in (url or "")), None)


def clean_article_content(content):
    lines = [l for l in (content or "").split("\n") if not any(n in l for n in UI_NOISE)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def scrape_article(url, paper):
    result = {"content": "", "author": ""}
    try:
        resp = requests.get(url, headers=HEADERS_WEB, timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        cfg  = PAPER_CONFIG.get(paper, {})
        for sel in cfg.get("body", []) + DEFAULT_BODY:
            tag = soup.select_one(sel)
            if tag:
                text = tag.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    result["content"] = text
                    break
        for sel in cfg.get("author", []) + DEFAULT_AUTHOR:
            tag = soup.select_one(sel)
            if tag:
                author = tag.get_text(strip=True)
                if author and len(author) < 30:
                    result["author"] = author
                    break
    except Exception as e:
        print(f"    크롤링 실패: {e}")
    return result


def enrich_news_with_content(news_list):
    """정치/경제/사회/국제/북한 등 뉴스 본문을 스크래핑해 텍스트 전용 원문용 content를 채웁니다."""
    for news in news_list or []:
        url = news.get("url", "")
        if not url:
            continue
        paper = detect_paper(url) or ""
        scraped = scrape_article(url, paper)
        content = clean_article_content(scraped.get("content", "") or news.get("desc", ""))
        news["content"] = content
        news["author"] = scraped.get("author", "") or news.get("author", "")
        news["paper"] = paper or news.get("category") or news.get("paper") or "뉴스"
        title = news.get("title", "")[:40]
        status = "본문" if scraped.get("content") else "요약대체"
        print(f"    ✓ 원문수집({status}) [{news['paper']}] {title}")
    return news_list


def get_editorials():
    """[사설] 키워드로 한번에 검색 후 신문사 도메인으로 분류"""
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    NOT_EDITORIAL = ["[단독]", "[인터뷰]", "학위복", "[속보]", "[포토]", "[영상]"]
    found = {}

    queries = ["[사설]", "사설 한겨레 조선일보", "신문사설 오늘"]

    for query in queries:
        if len(found) >= len(PAPERS):
            break
        try:
            resp = requests.get("https://openapi.naver.com/v1/search/news.json",
                                headers=headers,
                                params={"query": query, "display": 50, "sort": "date"},
                                timeout=10)
            items = resp.json().get("items", [])
            print(f"  쿼리 '{query}': {len(items)}개")

            for item in items:
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                link  = item.get("originallink") or item.get("link", "")
                desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                pub   = item.get("pubDate", "")

                # 신문사 판별 (사설은 5대 신문만)
                paper = detect_paper(link)
                if not paper or paper not in PAPERS or paper in found:
                    continue

                # 차단 도메인
                if any(b in link for b in BLOCKED_DOMAINS):
                    continue

                # 사설 아닌 것 제외
                if any(x in title for x in NOT_EDITORIAL):
                    continue

                # 시간 필터 (48시간)
                try:
                    pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                    if (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 48:
                        continue
                except:
                    pub_str = pub[:16] if pub else "시각 미상"

                # 본문: 스크래핑 실패시 description 사용
                scraped = scrape_article(link, paper)
                content = clean_article_content(scraped.get("content", "") or desc)
                author  = scraped.get("author", "") or "논설위원실"

                if title and content:
                    found[paper] = {"paper": paper, "title": title, "author": author,
                                    "pub": pub_str, "content": content, "url": link}
                    print(f"  ✓ [{paper}] {title[:40]}")

        except Exception as e:
            print(f"  오류({query}): {e}")

    # 각 신문사별로 개별 검색도 추가 시도 (못 찾은 신문사만)
    for paper in PAPERS:
        if paper in found:
            continue
        try:
            resp = requests.get("https://openapi.naver.com/v1/search/news.json",
                                headers=headers,
                                params={"query": f"{paper} 사설", "display": 50, "sort": "date"},
                                timeout=10)
            for item in resp.json().get("items", []):
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                link  = item.get("originallink") or item.get("link", "")
                desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                pub   = item.get("pubDate", "")

                allowed = PAPER_DOMAINS.get(paper, [])
                if not any(d in link for d in allowed):
                    continue
                if any(b in link for b in BLOCKED_DOMAINS):
                    continue
                if any(x in title for x in NOT_EDITORIAL):
                    continue

                try:
                    pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                    if (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 48:
                        continue
                except:
                    pub_str = pub[:16] if pub else "시각 미상"

                scraped = scrape_article(link, paper)
                content = clean_article_content(scraped.get("content", "") or desc)
                author  = scraped.get("author", "") or "논설위원실"

                if title and content:
                    found[paper] = {"paper": paper, "title": title, "author": author,
                                    "pub": pub_str, "content": content, "url": link}
                    print(f"  ✓ [{paper}] {title[:40]}")
                    break

        except Exception as e:
            print(f"  [{paper}] 개별검색 오류: {e}")

    editorials = [found[p] for p in PAPERS if p in found]
    print(f"  → 총 {len(editorials)}개 수집완료")
    return editorials


def get_trending_news():
    """정치/경제/사회/국제 주요 이슈 수집 - AI가 선별"""
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    print("  [주요 이슈] 수집 중...")

    categories = {
        "정치": ["대통령 국회 정치", "여당 야당 법안"],
        "경제": ["경제 금리 물가", "주식 환율 수출"],
        "사회": ["사회 사건 사고", "교육 복지 노동"],
        "국제": ["국제 외교 미국", "트럼프 중국 유럽"],
    }

    raw_news, seen = [], set()

    for cat, keywords in categories.items():
        cat_count = 0
        for keyword in keywords:
            if cat_count >= 2:
                break
            try:
                resp = requests.get("https://openapi.naver.com/v1/search/news.json",
                                    headers=headers,
                                    params={"query": keyword, "display": 5, "sort": "date"},
                                    timeout=10)
                for item in resp.json().get("items", []):
                    title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                    link  = item.get("originallink") or item.get("link", "")
                    desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                    pub   = item.get("pubDate", "")

                    if title in seen:
                        continue
                    try:
                        pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                        if (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 24:
                            continue
                        pub_str = pub_dt.strftime("%m/%d %H:%M")
                    except:
                        pub_str = ""

                    seen.add(title)
                    raw_news.append({"title": title, "desc": desc,
                                     "url": link, "pub": pub_str, "cat": cat})
                    cat_count += 1
                    break
            except Exception as e:
                print(f"    오류({keyword}): {e}")

    if not raw_news:
        return []

    if len(raw_news) <= 6:
        print(f"    → 선별 생략 (수집 {len(raw_news)}개)")
        return [{**n, "category": n["cat"]} for n in raw_news]

    # AI로 주요 이슈 선별
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")
    news_text  = "\n".join([f"{i+1}. [{n['cat']}][{n['pub']}] {n['title']} / {n['desc'][:60]} / {n['url']}"
                            for i, n in enumerate(raw_news)])

    prompt = f"""다음 뉴스에서 오늘 가장 이슈가 되는 주요 뉴스 최대 6개를 선별하세요.
{news_text}

각 카테고리(정치/경제/사회/국제)에서 골고루 선택하고 JSON만 응답:
[{{"title":"","desc":"한줄요약30자이내","url":"","pub":"","category":"정치 또는 경제 또는 사회 또는 국제"}}]"""

    result, provider = call_ai(prompt, gemini_key, groq_key, max_tokens=800, timeout=30)
    if result:
        print(f"    → {provider} 선별: {len(result)}개")
        return result

    return [{**n, "category": n["cat"]} for n in raw_news[:6]]


def get_sisain_books():
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    print("  [시사인] 검색 중...")
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    try:
        resp  = requests.get("https://openapi.naver.com/v1/search/news.json",
                             headers=headers,
                             params={"query": "시사인 새로 나온 책", "display": 5, "sort": "date"},
                             timeout=10)
        for item in resp.json().get("items", []):
            title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            link  = item.get("originallink") or item.get("link", "")
            desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
            pub   = item.get("pubDate", "")
            if "시사인" not in title and "시사인" not in desc:
                continue
            try:
                pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                if (datetime.now(KST) - pub_dt).days > 7:
                    continue
                pub_str = pub_dt.strftime("%Y-%m-%d")
            except:
                pub_str = ""
            scraped = scrape_article(link, "시사인")
            content = scraped.get("content") or desc
            if title and content:
                print(f"    ✓ {title[:40]}")
                return {"title": title, "content": content[:3000], "url": link, "pub": pub_str}
        print("    ⚠️ 최근 7일 내 기사 없음")
    except Exception as e:
        print(f"    오류: {e}")
    return None


def get_security_news():
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    print("  [안보/전쟁] 검색 중...")
    keywords = ["전쟁", "분쟁 교전", "북한", "원유 에너지 안보", "해협", "핵 미사일", "연합뉴스 북한 전쟁"]
    naver_headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    raw_news, seen = [], set()

    for keyword in keywords:
        try:
            resp  = requests.get("https://openapi.naver.com/v1/search/news.json",
                                 headers=naver_headers,
                                 params={"query": keyword, "display": 5, "sort": "date"},
                                 timeout=10)
            for item in resp.json().get("items", []):
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                link  = item.get("originallink") or item.get("link", "")
                desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                pub   = item.get("pubDate", "")
                if title in seen:
                    continue
                try:
                    pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                    if (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 48:
                        continue
                    pub_str = pub_dt.strftime("%m/%d %H:%M")
                except:
                    pub_str = ""
                seen.add(title)
                raw_news.append({"title": title, "desc": desc, "url": link, "pub": pub_str})
        except Exception as e:
            print(f"    오류({keyword}): {e}")

    if not raw_news:
        return []

    if len(raw_news) <= 6:
        print(f"    → 선별 생략 (수집 {len(raw_news)}개)")
        return [{**n, "category": "안보/전쟁"} for n in raw_news]

    # AI 선별
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")
    news_text  = "\n".join([f"{i+1}. [{n['pub']}] {n['title']} / {n['desc'][:60]} / {n['url']}"
                            for i, n in enumerate(raw_news)])
    prompt = f"""뉴스 목록에서 안보/전쟁/경제안보 관련 최대 6개 선별. JSON만 응답:
{news_text}
선별 기준:
- 북한 관련 뉴스 (연합뉴스 포함 모든 출처 가능)
- 전쟁/분쟁 (우크라이나, 중동, 이란, 헤즈볼라 등)
- 경제안보 (호르무즈, 원유, 에너지, 반도체)
연합뉴스(yna.co.kr) 기사도 적극 포함할 것.
[{{"title":"","desc":"한줄요약","url":"","pub":"","category":"북한 또는 전쟁분쟁 또는 경제안보"}}]
해당없으면 []"""

    result, provider = call_ai(prompt, gemini_key, groq_key, max_tokens=1000, timeout=30)
    if result:
        print(f"    → {provider} 선별: {len(result)}개")
        return result

    return [{**n, "category":"안보/전쟁"} for n in raw_news[:6]]


def summarize_each(editorials):
    """각 사설별 AI 요약을 개별로 생성합니다."""
    if not editorials:
        return {}

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")

    summaries = {}
    for i, ed in enumerate(editorials):
        prompt = f"""다음 신문 사설을 요약하세요.

【{ed['paper']}】 {ed['title']}
{ed['content'][:1500]}

아래 JSON 객체 하나만 출력 (다른 텍스트 없이):
{{"index": {i}, "paper": "{ed['paper']}", "summary": "300자 이상 상세 요약. 핵심 주장과 근거를 구체적으로 서술.", "stance": "진보 또는 보수 또는 중도", "keywords": ["키워드1", "키워드2", "키워드3"]}}

반드시 한국어로만 작성하세요."""

        result, provider = call_ai(prompt, gemini_key, groq_key, max_tokens=2500, timeout=60, prefer="gemini")

        if isinstance(result, list) and result:
            result = result[0]
        if isinstance(result, dict) and result.get("summary"):
            summaries[ed["paper"]] = result
            print(f"    ✓ [{ed['paper']}] {provider} 요약 완료")
        else:
            print(f"    ✗ [{ed['paper']}] 요약 실패")

        if i < len(editorials) - 1:
            _groq_cooldown(provider)

    print(f"    → {len(summaries)}개 사설 요약 완료")
    return summaries


def summarize_news_items(news_list, label="뉴스"):
    """뉴스 항목별 AI 요약 (주요 이슈, 북한/전쟁 등)."""
    if not news_list:
        return {}

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")
    summaries = {}

    for i, news in enumerate(news_list):
        content = news.get("content") or news.get("desc", "")
        if not content.strip():
            continue

        cat = news.get("category") or news.get("cat", "일반")
        prompt = f"""다음 뉴스 기사를 요약하세요.

[{cat}] {news['title']}
{content[:1500]}

JSON 객체 하나만 출력 (다른 텍스트 없이):
{{"summary": "200자 이상 핵심 요약. 사실관계와 배경을 구체적으로 서술.", "keywords": ["키워드1", "키워드2", "키워드3"]}}

반드시 한국어로만 작성하세요."""

        result, provider = call_ai(prompt, gemini_key, groq_key, max_tokens=1200, timeout=45, prefer="gemini")

        if isinstance(result, list) and result:
            result = result[0]
        key = news.get("url") or news.get("title", str(i))
        if isinstance(result, dict) and result.get("summary"):
            summaries[key] = result
            print(f"    ✓ [{cat}] {provider} 요약 완료")
        else:
            print(f"    ✗ [{cat}] {news['title'][:30]} 요약 실패")

        if i < len(news_list) - 1:
            _groq_cooldown(provider)

    print(f"    → {len(summaries)}개 {label} 요약 완료")
    return summaries


def _news_summary_html(news, news_summaries):
    ai = news_summaries.get(news.get("url", ""), {})
    ai_summary = escape(ai.get("summary", ""))
    if ai_summary:
        keywords = escape(" ".join([f"#{k}" for k in ai.get("keywords", [])]))
        return f"""
  <div style="background:#f0f4f8;border-radius:6px;padding:12px 14px;margin-bottom:8px;">
    <div style="font-size:11px;color:#333;font-weight:bold;margin-bottom:6px;">🤖 AI 요약</div>
    <div style="font-size:14px;line-height:1.75;color:#333;">{ai_summary}</div>
    <div style="font-size:12px;color:#888;margin-top:6px;">{keywords}</div>
  </div>"""
    return f"""
  <div style="font-size:13px;color:#555;line-height:1.6;margin-bottom:8px;">
    {escape(news.get('desc', ''))}
  </div>"""


PAPER_SLUGS = {
    "한겨레": "hani",
    "조선일보": "chosun",
    "동아일보": "donga",
    "경향신문": "khan",
    "중앙일보": "joongang",
    "연합뉴스": "yna",
    "한국경제": "hankyung",
    "매일경제": "mk",
    "머니투데이": "mt",
    "시사인": "sisain",
    "정치": "politics",
    "경제": "economy",
    "사회": "society",
    "국제": "world",
    "북한": "nk",
    "전쟁분쟁": "conflict",
    "경제안보": "econ-security",
}


def safe_slug(value):
    slug = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "-", value).strip("-")
    return slug.lower() or "article"


def article_date_key(edition, start):
    suffix = "morning" if "아침" in edition else "evening"
    return f"{start.strftime('%Y-%m-%d')}-{suffix}"


def cleanup_old_article_pages(days=30):
    originals_dir = Path("originals")
    if not originals_dir.exists():
        return
    cutoff = datetime.now(KST).date() - timedelta(days=days)
    for path in originals_dir.iterdir():
        if not path.is_dir():
            continue
        match = re.match(r"(\d{4}-\d{2}-\d{2})-(morning|evening)$", path.name)
        if not match:
            continue
        try:
            folder_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(path)
            print(f"  오래된 원문 페이지 삭제: {path}")


def build_article_pages(articles, edition, start):
    """사설/뉴스 본문을 광고 없는 GitHub Pages용 텍스트 전용 HTML로 저장합니다."""
    if not articles:
        return

    base_url = os.environ.get("BASE_PAGES_URL", "https://acts1615.github.io/editorial-bot").rstrip("/")
    date_key = article_date_key(edition, start)
    output_dir = Path("originals") / date_key
    output_dir.mkdir(parents=True, exist_ok=True)

    for ed in articles:
        paper = ed.get("paper") or ed.get("category") or "뉴스"
        digest = hashlib.sha256(ed.get("url", "").encode("utf-8")).hexdigest()[:10]
        paper_slug = PAPER_SLUGS.get(paper, safe_slug(paper))
        filename = f"{paper_slug}-{digest}.html"
        page_path = output_dir / filename
        page_url = f"{base_url}/originals/{date_key}/{filename}"
        ed["text_page_url"] = page_url

        title = escape(ed.get("title", "제목 없음"))
        paper_html = escape(paper)
        author = escape(ed.get("author", ""))
        pub = escape(ed.get("pub", ""))
        content = escape(ed.get("content", "")).strip()
        source_url = escape(ed.get("url", ""))
        generated_at = escape(datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"))
        meta_bits = " &nbsp;·&nbsp; ".join(x for x in [f"✍️ {author}" if author else "", pub] if x)
        meta_html = f'<div class="meta">{meta_bits}</div>' if meta_bits else ""

        page_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | 잡다한 사설들</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #f5f5f5;
    color: #222;
    font-family: 'Malgun Gothic', Apple SD Gothic Neo, sans-serif;
    line-height: 1.8;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 20px; }}
  .card {{ background: #fff; border-radius: 14px; padding: 24px; box-shadow: 0 2px 14px rgba(0,0,0,.08); }}
  .badge {{ display: inline-block; background: #1a3a5c; color: #fff; border-radius: 20px; padding: 3px 12px; font-size: 13px; font-weight: bold; }}
  h1 {{ font-size: 24px; line-height: 1.45; margin: 14px 0 8px; }}
  .meta {{ color: #777; font-size: 13px; margin-bottom: 18px; }}
  .notice {{ background: #f0f4f8; border-radius: 8px; padding: 12px 14px; color: #456; font-size: 13px; margin-bottom: 20px; }}
  pre {{ white-space: pre-wrap; word-break: keep-all; overflow-wrap: anywhere; margin: 0; font-family: inherit; font-size: 17px; line-height: 1.9; }}
  .source {{ margin-top: 28px; padding-top: 18px; border-top: 1px solid #eee; color: #666; font-size: 13px; }}
  .url {{ margin-top: 6px; padding: 10px; background: #fafafa; border: 1px solid #eee; border-radius: 6px; word-break: break-all; color: #333; }}
  .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 18px; }}
</style>
</head>
<body>
  <main class="wrap">
    <article class="card">
      <span class="badge">{paper_html}</span>
      <h1>{title}</h1>
      {meta_html}
      <div class="notice">광고 없이 읽을 수 있도록 이메일 발송 시점에 수집한 원문 텍스트만 표시합니다. 메일로 돌아가려면 브라우저의 뒤로가기를 누르세요.</div>
      <pre>{content}</pre>
      <div class="source">
        <strong>출처 URL</strong> <span style="color:#999;">(복사해서 브라우저에 붙여넣어야 원문 사이트로 이동할 수 있습니다)</span>
        <div class="url">{source_url}</div>
      </div>
    </article>
    <div class="footer">GitHub Actions + Gemini/Groq AI 자동 생성 | {generated_at}</div>
  </main>
</body>
</html>"""
        page_path.write_text(page_html, encoding="utf-8")

    cleanup_old_article_pages()
    print(f"  텍스트 전용 원문 페이지 {len(articles)}개 생성: {output_dir}")


def text_original_link_html(item, link_style="font-size:12px;color:#1a6ec8;text-decoration:none;font-weight:bold;"):
    """광고 없는 텍스트 원문 링크 + 출처 URL(복사용). text_page_url이 없으면 외부 URL로 폴백."""
    text_page_url = item.get("text_page_url")
    source_url = escape(item.get("url", ""))
    source_url_display = source_url.replace("://", "://<wbr>").replace(".", ".<wbr>")
    if text_page_url:
        href = escape(text_page_url, quote=True)
        return f"""
  <a href="{href}" style="{link_style}">📄 원문보기</a>
  <div style="font-size:11px;color:#777;line-height:1.5;margin-top:6px;word-break:break-all;">
    출처 URL (복사/붙여넣기용): <span style="color:#777;text-decoration:none;cursor:text;pointer-events:none;">{source_url_display}</span>
  </div>"""
    href = escape(item.get("url", ""), quote=True)
    return f'<a href="{href}" style="font-size:12px;color:#888;text-decoration:none;">🔗 원문 보기</a>'


def build_email(editorials, sisain, security_news, trending_news, summaries, edition, start, end, news_summaries=None):
    news_summaries = news_summaries or {}
    period   = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    dow = datetime.now(KST).strftime("%a").replace(
        "Mon","월").replace("Tue","화").replace("Wed","수").replace(
        "Thu","목").replace("Fri","금").replace("Sat","토").replace("Sun","일")
    md  = datetime.now(KST).strftime("%m/%d")
    판   = "오전판" if datetime.now(KST).hour < 12 else "저녁판"
    subject = f"📰 잡다한 사설들 | {md} ({dow}) {판}"

    # 북한/전쟁 소식
    security_html = ""
    if security_news:
        items_html = ""
        for news in security_news:
            cat = news.get("category", "안보")
            color = "#c62828" if "북한" in cat else "#e65100" if "전쟁" in cat else "#1565c0"
            items_html += f"""
<div style="padding:14px 16px;margin-bottom:10px;border-radius:8px;
            background:#fff;border:1px solid #eee;border-left:4px solid {color};">
  <div style="font-size:12px;color:{color};margin-bottom:5px;font-weight:bold;">
    {cat} · {news['pub']}
  </div>
  <div style="font-size:15px;font-weight:bold;color:#1a1a1a;margin-bottom:6px;line-height:1.4;">
    {news['title']}
  </div>
  {_news_summary_html(news, news_summaries)}
  {text_original_link_html(news)}
</div>"""
        security_html = f"""
<h2 style="font-size:18px;color:#c62828;border-bottom:2px solid #c62828;
           padding-bottom:8px;margin:28px 0 16px;">🚨 북한/전쟁 주요 소식</h2>
{items_html}"""

    # 시사인
    # 오늘의 주요 이슈
    trending_html = ""
    if trending_news:
        cat_colors = {"정치":"#8e24aa","경제":"#1565c0","사회":"#2e7d32","국제":"#e65100"}
        items_html = ""
        for news in trending_news:
            cat   = news.get("category","일반")
            color = cat_colors.get(cat, "#555")
            items_html += f"""
<div style="padding:14px 16px;margin-bottom:10px;border-radius:8px;
            background:#fff;border:1px solid #eee;border-left:4px solid {color};">
  <div style="font-size:12px;color:{color};margin-bottom:5px;font-weight:bold;">
    {cat} · {news['pub']}
  </div>
  <div style="font-size:15px;font-weight:bold;color:#1a1a1a;margin-bottom:6px;line-height:1.4;">
    {news['title']}
  </div>
  {_news_summary_html(news, news_summaries)}
  {text_original_link_html(news)}
</div>"""
        trending_html = f"""
<h2 style="font-size:18px;color:#333;border-bottom:2px solid #333;
           padding-bottom:8px;margin:28px 0 16px;">📌 오늘의 주요 이슈</h2>
{items_html}"""

    sisain_html = ""
    if sisain:
        paras = "".join(
            f"<p style='margin:0 0 14px;font-size:15px;line-height:1.85;color:#1a1a1a;text-indent:1em;'>{p}</p>"
            for p in sisain["content"].split("\n") if p.strip() and len(p.strip()) > 10
        )
        sisain_html = f"""
<h2 style="font-size:18px;color:#2d6a2d;border-bottom:2px solid #2d6a2d;
           padding-bottom:8px;margin:28px 0 16px;">📚 시사인 — 새로 나온 책</h2>
<div style="border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);margin-bottom:24px;">
  <div style="background:#2d6a2d;padding:14px 20px;">
    <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-bottom:4px;">시사인 · {sisain['pub']}</div>
    <h3 style="margin:0;font-size:16px;color:#fff;">{sisain['title']}</h3>
  </div>
  <div style="background:#fffef9;padding:20px 24px;">
    {paras}
    <a href="{sisain['url']}" style="font-size:13px;color:#888;">🔗 원문 보기</a>
  </div>
</div>"""

    # 사설 (신문사별: 헤더 → AI요약 → 텍스트 전용 원문 링크)
    editorial_blocks = ""
    for ed in editorials:
        ai = summaries.get(ed["paper"], {})
        paper      = escape(ed.get("paper", ""))
        pub        = escape(ed.get("pub", ""))
        title      = escape(ed.get("title", ""))
        author     = escape(ed.get("author", ""))
        ai_summary = escape(ai.get("summary", "요약을 불러올 수 없습니다."))
        stance     = escape(ai.get("stance", ""))
        keywords   = escape(" ".join([f"#{k}" for k in ai.get("keywords", [])]))
        editorial_blocks += f"""
<div style="padding:16px 18px;margin-bottom:12px;border-radius:10px;
            background:#fff;border:1px solid #e0e0e0;border-left:4px solid #1a3a5c;">
  <!-- 신문사 / 날짜 -->
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
    <span style="background:#1a3a5c;color:#fff;font-size:12px;font-weight:bold;
                 padding:2px 10px;border-radius:20px;">{paper}</span>
    <span style="color:#999;font-size:12px;">{pub}</span>
  </div>
  <!-- 제목 -->
  <div style="font-size:16px;font-weight:bold;color:#1a1a1a;margin-bottom:4px;line-height:1.4;">
    {title}
  </div>
  <!-- 작성자 -->
  <div style="font-size:12px;color:#888;margin-bottom:10px;">✍️ {author}</div>
  <!-- AI 요약 -->
  <div style="background:#f0f4f8;border-radius:6px;padding:12px 14px;margin-bottom:10px;">
    <div style="font-size:11px;color:#1a3a5c;font-weight:bold;margin-bottom:6px;">🤖 AI 요약</div>
    <div style="font-size:14px;line-height:1.75;color:#333;">{ai_summary}</div>
    <div style="font-size:12px;color:#888;margin-top:6px;">
      논조: <strong>{stance}</strong> &nbsp;·&nbsp; {keywords}
    </div>
  </div>
  <!-- 텍스트 전용 원문 링크 / 실제 출처 URL -->
  {text_original_link_html(ed, link_style="font-size:13px;color:#1a6ec8;text-decoration:none;font-weight:bold;")}
</div>"""

    html = f"""<!DOCTYPE html><html lang="ko">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:'Malgun Gothic',sans-serif;max-width:700px;
             margin:0 auto;padding:20px;background:#f5f5f5;color:#222;">

  <!-- 메인 헤더 -->
  <div style="background:#1a3a5c;color:#fff;padding:20px 24px;
              border-radius:12px;margin-bottom:24px;text-align:center;">
    <div style="font-size:12px;opacity:.7;margin-bottom:4px;">{period}</div>
    <h1 style="margin:0 0 4px;font-size:24px;">📰 잡다한 사설들</h1>
    <div style="font-size:14px;opacity:.85;">{edition} · {date_str}</div>
  </div>

  <!-- 사설 섹션 -->
  <h2 style="font-size:18px;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:8px;margin:28px 0 20px;">📰 오늘의 사설</h2>
  {editorial_blocks}

  {trending_html}
  {sisain_html}

  {security_html}

  <!-- 구독 버튼 -->
  <div style="text-align:center;margin:32px 0 20px;">
    <a href="https://acts1615.github.io/editorial-bot/subscribe.html"
       style="display:inline-block;padding:12px 28px;background:#1a3a5c;color:#fff;
              text-decoration:none;border-radius:24px;font-size:15px;">
      📬 구독 신청 / 해지
    </a>
    <p style="color:#999;font-size:12px;margin-top:8px;">지인에게 공유해 보세요!</p>
  </div>

  <p style="color:#bbb;font-size:11px;text-align:center;">
    GitHub Actions + Gemini/Groq AI 자동 생성 | {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}
  </p>
</body></html>"""

    plain = f"[{edition}] {date_str} 사설 브리핑\n\n"
    for ed in editorials:
        ai = summaries.get(ed["paper"], {})
        plain += f"\n{'='*40}\n"
        plain += f"【{ed['paper']}】 {ed['title']}\n✍️ {ed['author']} | {ed['pub']}\n\n"
        plain += f"[AI 요약]\n{ai.get('summary','')}\n논조: {ai.get('stance','')} | {' '.join(['#'+k for k in ai.get('keywords',[])])}\n\n"
        plain += f"[원문보기 - 텍스트 전용]\n{ed.get('text_page_url', '')}\n\n"
        plain += f"[출처 URL - 복사/붙여넣기용]\n{ed['url']}\n\n"
        plain += f"[원문]\n{ed['content']}\n"

    return subject, html, plain


def get_subscribers():
    script_url = os.environ.get("APPS_SCRIPT_URL", "")
    subscribers = []
    if script_url:
        try:
            resp = requests.get(f"{script_url}?action=list", timeout=15)
            if resp.status_code == 200:
                text = resp.text.strip()
                if text and text.startswith("["):
                    data = resp.json()
                    subscribers = [item["email"] for item in data if item.get("email")]
                    print(f"   구글 시트 구독자: {len(subscribers)}명")
        except Exception as e:
            print(f"   구글 시트 오류: {e}")
    keys = ["RECIPIENT_EMAIL"] + [f"RECIPIENT_EMAIL{i}" for i in range(2, 11)]
    for key in keys:
        email = os.environ.get(key, "").strip()
        if email and email not in subscribers:
            subscribers.append(email)
    return subscribers


def send_gmail(subject, html, plain):
    sender     = os.environ["SENDER_EMAIL"]
    password   = os.environ["GMAIL_APP_PASSWORD"]
    recipients = get_subscribers()
    print(f"   총 수신자 {len(recipients)}명 발송 시작")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        for recipient in recipients:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = sender
            msg["To"]      = recipient
            msg.attach(MIMEText(plain, "plain", "utf-8"))
            msg.attach(MIMEText(html,  "html",  "utf-8"))
            server.sendmail(sender, recipient, msg.as_string())
            masked = recipient[:3] + "***@" + recipient.split("@")[-1] if "@" in recipient else "***"
            print(f"✅ 발송 완료 → {masked}")


if __name__ == "__main__":
    now = datetime.now(KST)
    print(f"\n{'='*55}")
    print(f"📰 신문 사설 봇 시작: {now.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*55}\n")

    start, end, edition = get_time_window()
    print(f"📅 수집 범위: {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')} ({edition})\n")

    print("① 사설 수집 중...")
    editorials = get_editorials()
    print(f"   → {len(editorials)}개 수집 완료\n")

    print("② 사설별 AI 요약 중...")
    summaries = summarize_each(editorials)
    print()

    print("③ 주요 이슈 수집 중...")
    trending_news = get_trending_news()
    print()

    print("④ 북한/전쟁 뉴스 수집 중...")
    security_news = get_security_news()
    print()

    print("⑤ 시사인 새로 나온 책 수집 중...")
    sisain = get_sisain_books()
    print()

    print("⑥ 뉴스 원문 수집 중...")
    enrich_news_with_content(trending_news)
    enrich_news_with_content(security_news)
    print()

    print("⑦ 주요 이슈 AI 요약 중...")
    news_summaries = summarize_news_items(trending_news, "주요 이슈")
    print()

    print("⑧ 북한/전쟁 AI 요약 중...")
    news_summaries.update(summarize_news_items(security_news, "북한/전쟁"))
    print()

    print("⑨ 텍스트 전용 원문 페이지 생성 중...")
    all_articles = list(editorials) + list(trending_news or []) + list(security_news or [])
    build_article_pages(all_articles, edition, start)
    print()

    print("⑩ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, sisain, security_news, trending_news, summaries, edition, start, end, news_summaries)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
