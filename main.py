"""
신문 사설 자동 요약 & 이메일 발송 봇 v5
- 한겨레 맨 앞 배치
- 시사인 새로 나온 책 주간 코너 추가
- 사설 요약 300자 이상 + 요약 바로 아래 원문 링크
- 주제별 분류에 원문 링크 추가
- 도메인 필터링 (사설 요약 사이트 차단)
- Gemini + Groq 백업 AI
- Google Sheets 구독자 연동
"""

import os, re, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

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

BLOCKED_DOMAINS = ["nongaek.com", "newsis.com", "news1.kr", "yna.co.kr", "pressian.com"]

HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
}

PAPER_CONFIG = {
    "한겨레":   {"body": [".article-text", ".text"], "author": [".byline strong"]},
    "조선일보": {"body": [".article-body"],           "author": [".article__author-name"]},
    "동아일보": {"body": [".article_txt"],            "author": [".reporter_name"]},
    "경향신문": {"body": [".art_body"],               "author": [".reporter_area .name"]},
    "중앙일보": {"body": [".article_body"],           "author": [".byline__name"]},
}
DEFAULT_BODY   = ["article", ".article", ".news_body", "#articleBody", "main", ".content"]
DEFAULT_AUTHOR = [".author", ".byline", ".reporter", "[rel='author']"]


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
        print(f"    본문 크롤링 실패: {e}")
    return result


def search_naver_editorial(paper):
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    query  = f"{paper} 사설"
    url    = "https://openapi.naver.com/v1/search/news.json"
    params = {"query": query, "display": 10, "sort": "date"}
    headers = {
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    try:
        resp  = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"    네이버 API 상태: {resp.status_code}")
        items = resp.json().get("items", [])
        print(f"    검색 결과: {len(items)}개")

        for item in items:
            title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            link  = item.get("originallink") or item.get("link", "")
            pub   = item.get("pubDate", "")

            # 사설 여부 확인 (제목 앞부분에 [사설] 또는 사설로 시작)
            if not title.startswith("[사설]") and "사설" not in title[:4]:
                continue

            # 차단 도메인 필터
            if any(blocked in link for blocked in BLOCKED_DOMAINS):
                continue

            # 신문사 공식 도메인 확인
            allowed = PAPER_DOMAINS.get(paper, [])
            if allowed and not any(domain in link for domain in allowed):
                continue

            try:
                pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
            except:
                pub_str = pub[:16] if pub else "시각 미상"

            scraped = scrape_article(link, paper) if link else {}
            content = scraped.get("content", "")
            author  = scraped.get("author", "") or "논설위원실"
            if len(content) < 100:
                content = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()

            if title and content:
                print(f"    ✓ [{pub_str}] {title[:40]}")
                return {"paper": paper, "title": title, "author": author,
                        "pub": pub_str, "content": content, "url": link}

        print(f"    ⚠️ 사설 없음")
    except Exception as e:
        print(f"    오류: {e}")
    return None


def get_editorials():
    editorials = []
    for paper in PAPERS:
        print(f"  [{paper}] 검색 중...")
        result = search_naver_editorial(paper)
        if result:
            editorials.append(result)
    return editorials


def get_sisain_books():
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    print("  [시사인] 새로 나온 책 검색 중...")
    query  = "시사인 새로 나온 책"
    url    = "https://openapi.naver.com/v1/search/news.json"
    params = {"query": query, "display": 5, "sort": "date"}
    headers = {
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    try:
        resp  = requests.get(url, headers=headers, params=params, timeout=10)
        items = resp.json().get("items", [])
        for item in items:
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


def summarize(editorials, sisain, edition, start, end):
    if not editorials:
        return "수집된 사설이 없습니다."

    api_key = os.environ["GEMINI_API_KEY"]
    period  = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"

    corpus = ""
    for ed in editorials:
        corpus += (
            f"\n\n【{ed['paper']}】 {ed['title']}\n"
            f"작성자: {ed['author']} | 발행: {ed['pub']}\n"
            f"원문URL: {ed['url']}\n"
            f"{ed['content'][:2500]}"
        )

    prompt = f"""다음은 {period} 주요 신문 사설들입니다.
{corpus}

아래 형식으로 정리해 주세요.

━━━━━━━━━━━━━━━━━━━━━━
📌 핵심 이슈 총평 (3줄 이내)
━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━
🗂 주제별 분류
━━━━━━━━━━━━━━━━━━━━━━
카테고리: [정치/외교] [경제/산업] [사회/교육] [국제/안보] [기타]
형식: ● [카테고리] 신문사 — 제목 → 원문URL

━━━━━━━━━━━━━━━━━━━━━━
📰 사설별 요약
━━━━━━━━━━━━━━━━━━━━━━
각 사설마다 아래 형식으로:
▶ [신문사] 제목
  • 주요 주장: (반드시 300자 이상으로 핵심 주장과 근거를 상세히 서술)
  • 논조: 진보/보수/중도
  • 키워드: #태그 #태그 #태그
  • 원문: 원문URL

한국어로만 작성하고, 사설별 요약은 반드시 300자 이상으로 작성해 주세요."""

    # 1순위: Gemini REST API
    gemini_candidates = [
        ("v1beta", "gemini-1.5-flash-latest"),
        ("v1",     "gemini-1.5-flash"),
        ("v1beta", "gemini-1.5-flash"),
        ("v1",     "gemini-1.5-pro"),
        ("v1beta", "gemini-1.5-pro-latest"),
    ]
    for ver, model in gemini_candidates:
        try:
            url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={api_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            resp = requests.post(url, json=payload, timeout=60)
            print(f"    [Gemini/{model}]: {resp.status_code}")
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                err = resp.json().get("error", {}).get("message", "")[:80]
                print(f"    오류: {err}")
        except Exception as e:
            print(f"    Gemini 예외: {e}")

    # 2순위: Groq
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        groq_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
        for model in groq_models:
            try:
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 3000}
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                print(f"    [Groq/{model}]: {resp.status_code}")
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                else:
                    err = resp.json().get("error", {}).get("message", "")[:80]
                    print(f"    오류: {err}")
            except E
