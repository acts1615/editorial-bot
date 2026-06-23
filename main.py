"""
신문 사설 자동 요약 & 이메일 발송 봇 v2026.06.23-1303
구조: 신문사/제목/작성자 → AI 요약 → 원문 (신문사별 개별 구성)
"""

import os, re, smtplib, json, hashlib, shutil
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

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
}
DEFAULT_BODY   = ["article", ".article", ".news_body", "#articleBody", "main", ".content"]
DEFAULT_AUTHOR = [".author", ".byline", ".reporter"]


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


def get_editorials():
    """[사설] 키워드로 한번에 검색 후 신문사 도메인으로 분류"""
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    DOMAIN_TO_PAPER = {
        "hani.co.kr":     "한겨레",
        "chosun.com":     "조선일보",
        "donga.com":      "동아일보",
        "khan.co.kr":     "경향신문",
        "joongang.co.kr": "중앙일보",
        "joins.com":      "중앙일보",
    }
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

                # 신문사 판별
                paper = next((p for d, p in DOMAIN_TO_PAPER.items() if d in link), None)
                if not paper or paper in found:
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
                content = scraped.get("content", "") or desc
                author  = scraped.get("author", "") or "논설위원실"

                # UI 노이즈 제거
                ui_noise = ["공유하기", "카카오톡으로 공유하기", "URL 복사", "창 닫기", "SNS"]
                lines = [l for l in content.split("\n") if not any(n in l for n in ui_noise)]
                content = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

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
                content = scraped.get("content", "") or desc
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

    # AI로 주요 이슈 선별
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")
    news_text  = "\n".join([f"{i+1}. [{n['cat']}][{n['pub']}] {n['title']} / {n['desc'][:60]} / {n['url']}"
                            for i, n in enumerate(raw_news)])

    prompt = f"""다음 뉴스에서 오늘 가장 이슈가 되는 주요 뉴스 최대 6개를 선별하세요.
{news_text}

각 카테고리(정치/경제/사회/국제)에서 골고루 선택하고 JSON만 응답:
[{{"title":"","desc":"한줄요약30자이내","url":"","pub":"","category":"정치 또는 경제 또는 사회 또는 국제"}}]"""

    for ver, model in [("v1beta","gemini-2.0-flash"),("v1beta","gemini-2.0-flash-lite")]:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={gemini_key}",
                json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=30)
            if r.status_code == 200:
                text = re.sub(r"```json|```","",r.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
                result = json.loads(text)
                print(f"    → Gemini 선별: {len(result)}개")
                return result
        except:
            pass

    if groq_key:
        for model in ["llama-3.3-70b-versatile","llama-3.1-8b-instant"]:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                    json={"model":model,"messages":[{"role":"user","content":prompt}],"max_tokens":800},
                    timeout=30)
                if r.status_code == 200:
                    text = re.sub(r"```json|```","",r.json()["choices"][0]["message"]["content"]).strip()
                    result = json.loads(text)
                    print(f"    → Groq 선별: {len(result)}개")
                    return result
            except:
                pass

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

    for ver, model in [("v1beta","gemini-2.0-flash"),("v1beta","gemini-2.0-flash-lite")]:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={gemini_key}",
                json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=30)
            if r.status_code == 200:
                text = re.sub(r"```json|```","",r.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
                result = json.loads(text)
                print(f"    → Gemini 선별: {len(result)}개")
                return result
        except:
            pass

    if groq_key:
        for model in ["llama-3.3-70b-versatile","llama-3.1-8b-instant"]:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                    json={"model":model,"messages":[{"role":"user","content":prompt}],"max_tokens":1000},
                    timeout=30)
                if r.status_code == 200:
                    text = re.sub(r"```json|```","",r.json()["choices"][0]["message"]["content"]).strip()
                    result = json.loads(text)
                    print(f"    → Groq 선별: {len(result)}개")
                    return result
            except:
                pass

    return [{**n, "category":"안보/전쟁"} for n in raw_news[:6]]


def summarize_each(editorials):
    """각 사설별 AI 요약을 개별로 생성합니다."""
    if not editorials:
        return {}

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")

    corpus = ""
    for i, ed in enumerate(editorials):
        corpus += f"\n\n[{i}] 【{ed['paper']}】 {ed['title']}\n{ed['content'][:2000]}"

    prompt = f"""다음 신문 사설들을 각각 요약해 주세요.

{corpus}

JSON 형식으로만 응답 (다른 텍스트 없이):
[
  {{
    "index": 0,
    "paper": "신문사명",
    "summary": "300자 이상 상세 요약. 핵심 주장과 근거를 구체적으로 서술.",
    "stance": "진보/보수/중도",
    "keywords": ["키워드1", "키워드2", "키워드3"]
  }}
]

반드시 한국어로만 작성하세요."""

    def call_ai(prompt):
        # Gemini
        for ver, model in [("v1beta","gemini-2.0-flash"),("v1beta","gemini-2.0-flash-lite")]:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={gemini_key}",
                    json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=60)
                if r.status_code == 200:
                    text = re.sub(r"```json|```","",r.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
                    return json.loads(text)
            except:
                pass
        # Groq
        if groq_key:
            for model in ["llama-3.3-70b-versatile","llama-3.1-8b-instant"]:
                try:
                    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                        json={"model":model,"messages":[{"role":"user","content":prompt}],"max_tokens":3000},
                        timeout=60)
                    if r.status_code == 200:
                        text = re.sub(r"```json|```","",r.json()["choices"][0]["message"]["content"]).strip()
                        return json.loads(text)
                except:
                    pass
        return None

    result = call_ai(prompt)
    if not result:
        return {}

    summaries = {}
    for item in result:
        idx = item.get("index", -1)
        if 0 <= idx < len(editorials):
            summaries[editorials[idx]["paper"]] = item
    print(f"    → {len(summaries)}개 사설 요약 완료")
    return summaries


PAPER_SLUGS = {
    "한겨레": "hani",
    "조선일보": "chosun",
    "동아일보": "donga",
    "경향신문": "khan",
    "중앙일보": "joongang",
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


def build_article_pages(editorials, edition, start):
    """사설 본문을 광고 없는 GitHub Pages용 텍스트 전용 HTML로 저장합니다."""
    if not editorials:
        return

    base_url = os.environ.get("BASE_PAGES_URL", "https://acts1615.github.io/editorial-bot").rstrip("/")
    date_key = article_date_key(edition, start)
    output_dir = Path("originals") / date_key
    output_dir.mkdir(parents=True, exist_ok=True)

    for ed in editorials:
        paper = ed.get("paper", "신문")
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
      <div class="meta">✍️ {author} &nbsp;·&nbsp; {pub}</div>
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
    print(f"  텍스트 전용 원문 페이지 {len(editorials)}개 생성: {output_dir}")


def build_email(editorials, sisain, security_news, trending_news, summaries, edition, start, end):
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
  <div style="font-size:13px;color:#555;line-height:1.6;margin-bottom:8px;">
    {news.get('desc','')}
  </div>
  <a href="{news['url']}" style="font-size:12px;color:#888;text-decoration:none;">🔗 원문 보기</a>
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
  <div style="font-size:13px;color:#555;line-height:1.6;margin-bottom:8px;">
    {news.get('desc','')}
  </div>
  <a href="{news['url']}" style="font-size:12px;color:#888;text-decoration:none;">🔗 원문 보기</a>
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
        text_page_url = escape(ed.get("text_page_url", ed.get("url", "")), quote=True)
        source_url = escape(ed.get("url", ""))
        source_url_display = source_url.replace("://", "://<wbr>").replace(".", ".<wbr>")

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
  <a href="{text_page_url}" style="font-size:13px;color:#1a6ec8;text-decoration:none;font-weight:bold;">
    📄 원문보기
  </a>
  <div style="font-size:11px;color:#777;line-height:1.5;margin-top:6px;word-break:break-all;">
    출처 URL (복사/붙여넣기용): <span style="color:#777;text-decoration:none;cursor:text;pointer-events:none;">{source_url_display}</span>
  </div>
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

    print("② 주요 이슈 수집 중...")
    trending_news = get_trending_news()
    print()

    print("③ 북한/전쟁 뉴스 수집 중...")
    security_news = get_security_news()
    print()

    print("③ 시사인 새로 나온 책 수집 중...")
    sisain = get_sisain_books()
    print()

    print("④ 사설별 AI 요약 중...")
    summaries = summarize_each(editorials)
    print()

    print("⑤ 텍스트 전용 원문 페이지 생성 중...")
    build_article_pages(editorials, edition, start)
    print()

    print("⑥ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, sisain, security_news, trending_news, summaries, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
