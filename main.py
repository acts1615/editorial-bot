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

            # 사설 여부 확인 - 반드시 [사설]로 시작해야 함
            if not title.startswith("[사설]"):
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


def build_email(editorials, sisain, summary, edition, start, end):
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
        paras = "".join(
            f"<p style='margin:0 0 10px;'>{p}</p>"
            for p in ed["content"].split("\n") if p.strip()
        )
        cards += f"""
<div style="border:1px solid #e0e0e0;border-radius:8px;padding:20px;
            margin-bottom:24px;background:#fafafa;">
  <div style="margin-bottom:8px;">
    <span style="background:#1a3a5c;color:#fff;font-size:12px;font-weight:bold;
                 padding:3px 10px;border-radius:20px;">{ed['paper']}</span>
    <span style="color:#888;font-size:12px;margin-left:8px;">{ed['pub']}</span>
  </div>
  <h3 style="margin:0 0 6px;font-size:17px;color:#1a1a1a;">{ed['title']}</h3>
  <p style="margin:0 0 6px;color:#666;font-size:13px;">✍️ {ed['author']}</p>
  <a href="{ed['url']}" style="display:inline-block;margin-bottom:14px;
     font-size:13px;color:#1a6ec8;">🔗 원문 보기</a>
  <div style="font-size:15px;line-height:1.85;color:#333;
              border-top:1px solid #e8e8e8;padding-top:14px;">{paras}</div>
</div>"""

    sisain_html = ""
    if sisain:
        sisain_paras = "".join(
            f"<p style='margin:0 0 10px;'>{p}</p>"
            for p in sisain["content"].split("\n") if p.strip()
        )
        sisain_html = f"""
<h2 style="font-size:18px;color:#2d6a2d;border-bottom:2px solid #2d6a2d;
           padding-bottom:8px;margin:32px 0 20px;">📚 시사인 — 새로 나온 책</h2>
<div style="border:1px solid #c8e6c9;border-radius:8px;padding:20px;
            margin-bottom:24px;background:#f9fbe7;">
  <h3 style="margin:0 0 8px;font-size:16px;color:#1a1a1a;">{sisain['title']}</h3>
  <a href="{sisain['url']}" style="display:inline-block;margin-bottom:14px;
     font-size:13px;color:#2d6a2d;">🔗 원문 보기</a>
  <div style="font-size:15px;line-height:1.85;color:#333;
              border-top:1px solid #c8e6c9;padding-top:14px;">{sisain_paras}</div>
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

    print("② 시사인 새로 나온 책 수집 중...")
    sisain = get_sisain_books()
    print(f"   → {'수집 완료' if sisain else '없음'}\n")

    print("③ AI 요약 중...")
    summary = summarize(editorials, sisain, edition, start, end)
    print("   → 완료\n")

    print("④ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, sisain, summary, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
