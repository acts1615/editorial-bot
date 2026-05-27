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

# 한겨레 맨 앞 배치
PAPERS = ["한겨레", "조선일보", "동아일보", "경향신문", "중앙일보"]

# 신문사별 공식 도메인
PAPER_DOMAINS = {
    "한겨레":   ["hani.co.kr"],
    "조선일보": ["chosun.com"],
    "동아일보": ["donga.com"],
    "경향신문": ["khan.co.kr"],
    "중앙일보": ["joongang.co.kr", "joins.com"],
}

# 차단할 도메인 (사설 요약 사이트)
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
    params = {"query": query, "display": 50, "sort": "date"}
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

            # 차단 도메인 필터
            if any(blocked in link for blocked in BLOCKED_DOMAINS):
                continue

            # 공식 도메인 확인
            allowed = PAPER_DOMAINS.get(paper, [])
            is_official = allowed and any(domain in link for domain in allowed)

            # 사설 여부 확인 - 제목 또는 URL로 판단
            editorial_url_patterns = [
                "/opinion/editorial", "/arti/opinion/editorial",
                "/Opinion/article", "/news/Opinion",
                "/opinion/column", "/editorial/"
            ]
            is_editorial_url = any(pat in link for pat in editorial_url_patterns)
            is_editorial_title = title.startswith("[사설]") or title.startswith("사설")

            if not is_editorial_title and not is_editorial_url:
                continue

            # 명백히 사설이 아닌 것 제외
            not_editorial = ["[단독]", "[인터뷰]", "[기자수첩]", "학위복", "[속보]"]
            if any(x in title for x in not_editorial):
                continue

            try:
                pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                # 48시간 이상 된 기사 제외
                age_hours = (datetime.now(KST) - pub_dt).total_seconds() / 3600
                if age_hours > 48:
                    continue
            except:
                pub_str = pub[:16] if pub else "시각 미상"

            scraped = scrape_article(link, paper) if link else {}
            content = scraped.get("content", "")
            author  = scraped.get("author", "") or "논설위원실"
            if len(content) < 100:
                content = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()

            # UI 텍스트 제거 (공유하기, SNS 버튼 등)
            ui_noise = ["공유하기", "카카오톡으로 공유하기", "페이스북으로 공유하기",
                        "트위터로 공유하기", "URL 복사", "창 닫기", "SNS", "퍼가기"]
            lines = content.split("\n")
            lines = [l for l in lines if not any(noise in l for noise in ui_noise)]
            content = "\n".join(lines)
            content = re.sub(r"\n{3,}", "\n\n", content).strip()

            if title and content and len(content) > 50:
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
    """시사인 새로 나온 책 코너 - 네이버 뉴스 API로 주간 최신 기사 수집"""
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


def get_security_news():
    """안보/전쟁/분쟁/경제안보 뉴스 - 포괄 키워드 수집 후 AI 선별"""
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    print("  [안보/전쟁/경제] 뉴스 수집 중...")

    # 포괄적 키워드로 넓게 수집
    keywords = ["전쟁", "분쟁 교전", "북한", "원유 에너지 안보", "해협", "핵 미사일"]
    naver_headers = {
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    raw_news = []
    seen_titles = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=naver_headers,
                params={"query": keyword, "display": 5, "sort": "date"},
                timeout=10
            )
            items = resp.json().get("items", [])
            for item in items:
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                link  = item.get("originallink") or item.get("link", "")
                desc  = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                pub   = item.get("pubDate", "")

                if title in seen_titles:
                    continue

                try:
                    pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                    age_hours = (datetime.now(KST) - pub_dt).total_seconds() / 3600
                    if age_hours > 48:
                        continue
                    pub_str = pub_dt.strftime("%m/%d %H:%M")
                except:
                    pub_str = ""

                seen_titles.add(title)
                raw_news.append({
                    "title": title,
                    "desc":  desc,
                    "url":   link,
                    "pub":   pub_str,
                })
        except Exception as e:
            print(f"    오류 ({keyword}): {e}")

    print(f"    → 원시 {len(raw_news)}개 수집, AI 선별 중...")

    if not raw_news:
        return []

    # AI로 중요 뉴스 선별 및 카테고리 분류
    import json
    news_text = "\n".join([
        f"{i+1}. [{n['pub']}] {n['title']} / {n['desc'][:80]} / {n['url']}"
        for i, n in enumerate(raw_news)
    ])

    prompt = f"""다음 뉴스 목록에서 안보/전쟁/경제안보 관련 중요 뉴스 최대 6개를 선별하세요.

{news_text}

선별 기준:
- 북한 (미사일, 핵, 도발, 군사 동향)
- 전쟁/분쟁 (우크라이나, 중동, 가자지구, 이란, 대만, 남중국해 등 어떤 지역이든)
- 경제안보 (호르무즈, 원유, 에너지, 반도체 수출규제, 미중 무역전쟁)

위 기준에 해당하는 것만 선별하고 JSON 배열로만 응답하세요 (다른 텍스트 없이):
[{{"title":"제목","desc":"한줄요약50자이내","url":"URL","pub":"시각","category":"북한 또는 전쟁분쟁 또는 경제안보"}}]
해당 없으면: []"""

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")

    # Gemini 시도
    for ver, model in [("v1beta", "gemini-2.0-flash"), ("v1beta", "gemini-2.0-flash-lite")]:
        try:
            url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={gemini_key}"
            resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
            if resp.status_code == 200:
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                text = re.sub(r"```json|```", "", text).strip()
                result = json.loads(text)
                print(f"    → AI 선별 완료 (Gemini): {len(result)}개")
                return result
        except Exception:
            pass

    # Groq 시도
    if groq_key:
        for model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
            try:
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1000}
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    text = re.sub(r"```json|```", "", text).strip()
                    result = json.loads(text)
                    print(f"    → AI 선별 완료 (Groq): {len(result)}개")
                    return result
            except Exception:
                pass

    # AI 실패시 원본 그대로 (최대 6개)
    print("    → AI 선별 실패, 원본 반환")
    return [{**n, "category": "안보/전쟁"} for n in raw_news[:6]]


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

반드시 한국어로만 작성하세요. 한자, 일본어, 힌디어, 영어 등 다른 언어 문자를 절대 사용하지 마세요.
사설별 요약은 반드시 300자 이상으로 작성해 주세요."""

    # 1순위: Gemini REST API
    gemini_candidates = [
        ("v1beta", "gemini-2.0-flash"),
        ("v1beta", "gemini-2.0-flash-lite"),
        ("v1beta", "gemini-1.5-flash-8b"),
        ("v1beta", "gemini-1.0-pro"),
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

    # 2순위: Groq (Gemini 실패시 자동 전환)
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
            except Exception as e:
                print(f"    Groq 예외: {e}")
    else:
        print("    Groq 키 없음 - GROQ_API_KEY Secret 확인 필요")

    return "AI 요약 실패 - 원문을 직접 확인해 주세요."


def build_email(editorials, sisain, security_news, summary, edition, start, end):
    period   = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    dow = datetime.now(KST).strftime("%a").replace(
        "Mon","월").replace("Tue","화").replace("Wed","수").replace(
        "Thu","목").replace("Fri","금").replace("Sat","토").replace("Sun","일")
    md  = datetime.now(KST).strftime("%m/%d")
    판   = "오전판" if datetime.now(KST).hour < 12 else "저녁판"
    subject = f"📰 잡다한 사설들 | {md} ({dow}) {판}"

    summary_html = summary.replace("\n", "<br>")

    cards = ""
    for ed in editorials:
        # 문단 단위로 분리하여 깔끔하게 표시
        paragraphs = [p.strip() for p in ed["content"].split("\n") if p.strip() and len(p.strip()) > 10]
        paras_html = "".join(
            f"<p style='margin:0 0 16px;font-size:16px;line-height:1.9;color:#1a1a1a;"
            f"text-indent:1em;'>{p}</p>"
            for p in paragraphs
        )
        cards += f"""
<div style="margin-bottom:40px;border-radius:12px;overflow:hidden;
            box-shadow:0 2px 12px rgba(0,0,0,0.08);">
  <!-- 헤더 -->
  <div style="background:#1a3a5c;padding:16px 20px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:12px;
                   font-weight:bold;padding:2px 10px;border-radius:20px;">{ed['paper']}</span>
      <span style="color:rgba(255,255,255,0.7);font-size:12px;">{ed['pub']}</span>
    </div>
    <h2 style="margin:0 0 6px;font-size:18px;color:#fff;line-height:1.4;
               font-weight:bold;">{ed['title']}</h2>
    <span style="color:rgba(255,255,255,0.7);font-size:13px;">✍️ {ed['author']}</span>
  </div>
  <!-- 본문 - 리더 모드 -->
  <div style="background:#fffef9;padding:24px 28px;">
    {paras_html}
    <div style="border-top:1px solid #eee;padding-top:12px;margin-top:8px;">
      <a href="{ed['url']}" style="font-size:13px;color:#888;text-decoration:none;">
        🔗 광고 없이 원문 보기
      </a>
    </div>
  </div>
</div>"""

    # 북한/전쟁 뉴스 섹션 - 리더 모드
    security_html = ""
    if security_news:
        news_items_html = ""
        for news in security_news:
            # 전문 스크래핑
            scraped = scrape_article(news["url"], "")
            full_content = scraped.get("content") or news.get("desc", "")
            news_paras = "".join(
                f"<p style='margin:0 0 16px;font-size:15px;line-height:1.9;color:#1a1a1a;text-indent:1em;'>{p}</p>"
                for p in full_content.split("\n") if p.strip() and len(p.strip()) > 10
            ) or f"<p style='font-size:15px;color:#555;'>{news.get('desc','')}</p>"

            cat = news.get("category", "안보/전쟁")
            cat_color = "#c62828" if "북한" in cat else "#e65100" if "전쟁" in cat else "#1565c0"

            news_items_html += f"""
<div style="margin-bottom:32px;border-radius:12px;overflow:hidden;
            box-shadow:0 2px 12px rgba(0,0,0,0.08);">
  <div style="background:{cat_color};padding:14px 20px;">
    <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-bottom:6px;">
      {cat} · {news['pub']}
    </div>
    <h3 style="margin:0;font-size:16px;color:#fff;line-height:1.4;">{news['title']}</h3>
  </div>
  <div style="background:#fffef9;padding:20px 24px;">
    {news_paras}
    <div style="border-top:1px solid #eee;padding-top:10px;margin-top:4px;">
      <a href="{news['url']}" style="font-size:13px;color:#888;text-decoration:none;">
        🔗 광고 없이 원문 보기
      </a>
    </div>
  </div>
</div>"""

        security_html = f"""
<h2 style="font-size:18px;color:#c62828;border-bottom:2px solid #c62828;
           padding-bottom:8px;margin:32px 0 20px;">🚨 북한/전쟁 주요 소식</h2>
{news_items_html}"""

    sisain_html = ""
    if sisain:
        sisain_paras = "".join(
            f"<p style='margin:0 0 16px;font-size:15px;line-height:1.9;color:#1a1a1a;text-indent:1em;'>{p}</p>"
            for p in sisain["content"].split("\n") if p.strip() and len(p.strip()) > 10
        )
        sisain_html = f"""
<h2 style="font-size:18px;color:#2d6a2d;border-bottom:2px solid #2d6a2d;
           padding-bottom:8px;margin:32px 0 20px;">📚 시사인 — 새로 나온 책</h2>
<div style="margin-bottom:32px;border-radius:12px;overflow:hidden;
            box-shadow:0 2px 12px rgba(0,0,0,0.08);">
  <div style="background:#2d6a2d;padding:14px 20px;">
    <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-bottom:6px;">
      시사인 · {sisain['pub']}
    </div>
    <h3 style="margin:0;font-size:16px;color:#fff;line-height:1.4;">{sisain['title']}</h3>
  </div>
  <div style="background:#fffef9;padding:20px 24px;">
    {sisain_paras}
    <div style="border-top:1px solid #eee;padding-top:10px;margin-top:4px;">
      <a href="{sisain['url']}" style="font-size:13px;color:#888;text-decoration:none;">
        🔗 광고 없이 원문 보기
      </a>
    </div>
  </div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="ko">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:'Malgun Gothic',sans-serif;max-width:700px;
             margin:0 auto;padding:20px;background:#fff;color:#222;">
  <div style="background:#1a3a5c;color:#fff;padding:20px 24px;
              border-radius:8px;margin-bottom:28px;">
    <div style="font-size:13px;opacity:.8;">{period}</div>
    <h1 style="margin:6px 0 0;font-size:22px;">📰 잡다한 사설들</h1>
    <div style="margin-top:6px;font-size:14px;opacity:.9;">{edition} · {date_str}</div>
  </div>
  <div style="background:#f0f4f8;border-left:4px solid #1a3a5c;
              padding:20px;border-radius:4px;margin-bottom:32px;">
    <h2 style="margin:0 0 14px;font-size:16px;color:#1a3a5c;">🤖 AI 요약 브리핑</h2>
    <div style="line-height:1.85;font-size:14px;">{summary_html}</div>
  </div>
  {sisain_html}
  {security_html}
  <h2 style="font-size:18px;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:8px;margin-bottom:20px;">📄 사설 원문</h2>
  {cards}
  <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
  <div style="text-align:center;margin-bottom:16px;">
    <a href="https://acts1615.github.io/editorial-bot/subscribe.html"
       style="display:inline-block;padding:10px 24px;background:#1a3a5c;color:#fff;
               text-decoration:none;border-radius:20px;font-size:14px;">
      📬 구독 신청 / 해지
    </a>
    <p style="color:#999;font-size:12px;margin-top:8px;">
      지인에게 공유해 보세요!
    </p>
  </div>
  <p style="color:#bbb;font-size:11px;text-align:center;">
    GitHub Actions + Gemini/Groq AI 자동 생성 | {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}
  </p>
</body></html>"""

    plain = f"[{edition}] {date_str} 사설 브리핑\n\n{summary}\n\n"
    if sisain:
        plain += f"\n📚 시사인 새로 나온 책\n{sisain['title']}\n{sisain['url']}\n\n{sisain['content']}\n\n{'─'*40}\n"
    for ed in editorials:
        plain += f"\n■ [{ed['paper']}] {ed['title']}\n작성자: {ed['author']}\n{ed['url']}\n\n{ed['content']}\n\n{'─'*40}\n"

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
                else:
                    print(f"   구글 시트 응답 비어있음 (무시)")
        except Exception as e:
            print(f"   구글 시트 오류: {e}")

    keys = ["RECIPIENT_EMAIL"] + [f"RECIPIENT_EMAIL{i}" for i in range(2, 11)]
    for key in keys:
        email = os.environ.get(key, "").strip()
        if email and email not in subscribers:
            subscribers.append(email)
    return subscribers


def send_gmail(subject, html, plain):
    sender    = os.environ["SENDER_EMAIL"]
    password  = os.environ["GMAIL_APP_PASSWORD"]
    recipients = get_subscribers()
    print(f"   총 수신자 {len(recipients)}명: {', '.join(recipients)}")
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
            print(f"✅ 발송 완료 → {recipient}")


if __name__ == "__main__":
    now = datetime.now(KST)
    print(f"\n{'='*55}")
    print(f"📰 신문 사설 봇 시작: {now.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*55}\n")

    start, end, edition = get_time_window()
    print(f"📅 수집 범위: {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')} ({edition})\n")

    print("① 사설 수집 중 (네이버 뉴스 API)...")
    editorials = get_editorials()
    print(f"\n   → 총 {len(editorials)}개 수집 완료\n")

    print("② 북한/전쟁 뉴스 수집 중...")
    security_news = get_security_news()
    print()
    print("③ 시사인 새로 나온 책 수집 중...")
    sisain = get_sisain_books()
    print(f"   → {'수집 완료' if sisain else '없음'}\n")

    print("④ AI 요약 중...")
    summary = summarize(editorials, sisain, edition, start, end)
    print("   → 완료\n")

    print("⑤ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, sisain, security_news, summary, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
