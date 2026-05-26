"""
신문 사설 자동 요약 & 이메일 발송 봇 v4
- 네이버 뉴스 검색 API 사용 (공식 API, 차단 없음, 무료)
- Gemini AI 요약
- 아침/저녁 2회 배달
"""

import os, re, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google import genai
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

# 검색할 신문사 목록
PAPERS = ["조선일보", "동아일보", "한겨레", "경향신문", "중앙일보"]

HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
}

PAPER_CONFIG = {
    "조선일보": {"body": [".article-body"], "author": [".article__author-name"]},
    "동아일보": {"body": [".article_txt"],  "author": [".reporter_name"]},
    "한겨레":   {"body": [".article-text", ".text"], "author": [".byline strong"]},
    "경향신문": {"body": [".art_body"],     "author": [".reporter_area .name"]},
    "중앙일보": {"body": [".article_body"], "author": [".byline__name"]},
}
DEFAULT_BODY   = ["article", ".article", ".news_body", "#articleBody", "main", ".content"]
DEFAULT_AUTHOR = [".author", ".byline", ".reporter", "[rel='author']"]


def scrape_article(url, paper):
    """기사 URL에서 본문과 작성자를 추출합니다."""
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
    """네이버 뉴스 API로 신문사 사설을 검색합니다."""
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    query = f"{paper} 사설"
    url   = "https://openapi.naver.com/v1/search/news.json"
    params = {
        "query":  query,
        "display": 5,
        "sort":   "date",
    }
    headers = {
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"    네이버 API 상태: {resp.status_code}")
        data  = resp.json()
        items = data.get("items", [])
        print(f"    검색 결과: {len(items)}개")

        for item in items:
            title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            link  = item.get("originallink") or item.get("link", "")
            pub   = item.get("pubDate", "")

            # 사설 여부 확인 (제목에 [사설] 포함)
            if "[사설]" not in title and "사설" not in title[:10]:
                continue

            # 발행 시각 파싱
            try:
                pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
            except:
                pub_str = pub[:16] if pub else "시각 미상"

            # 본문 크롤링
            scraped = scrape_article(link, paper) if link else {}
            content = scraped.get("content", "")
            author  = scraped.get("author", "") or "논설위원실"

            # 본문이 너무 짧으면 네이버 요약문 사용
            if len(content) < 100:
                content = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()

            if title and content:
                print(f"    ✓ [{pub_str}] {title[:40]}")
                return {
                    "paper":   paper,
                    "title":   title,
                    "author":  author,
                    "pub":     pub_str,
                    "content": content,
                    "url":     link,
                }

        print(f"    ⚠️ 사설 없음")
    except Exception as e:
        print(f"    오류: {e}")

    return None


def get_editorials():
    """전체 신문사 사설을 수집합니다."""
    editorials = []
    for paper in PAPERS:
        print(f"  [{paper}] 검색 중...")
        result = search_naver_editorial(paper)
        if result:
            editorials.append(result)
    return editorials


def summarize(editorials, edition, start, end):
    """Gemini AI로 요약합니다."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    if not editorials:
        return "수집된 사설이 없습니다."

    period = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    corpus = ""
    for ed in editorials:
        corpus += (
            f"\n\n【{ed['paper']}】 {ed['title']}\n"
            f"작성자: {ed['author']} | 발행: {ed['pub']}\n"
            f"{ed['content'][:2500]}\n출처: {ed['url']}"
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
형식: ● [카테고리] 신문사 — 제목

━━━━━━━━━━━━━━━━━━━━━━
📰 사설별 요약
━━━━━━━━━━━━━━━━━━━━━━
▶ [신문사] 제목
  • 주요 주장: (2문장)
  • 논조: 진보/보수/중도
  • 키워드: #태그 #태그 #태그

한국어로만, 5분 안에 읽을 수 있게 간결하게 써 주세요."""

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )
    return response.text


def build_email(editorials, summary, edition, start, end):
    period   = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    date_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    dow = datetime.now(KST).strftime("%a").replace(
        "Mon","월").replace("Tue","화").replace("Wed","수").replace(
        "Thu","목").replace("Fri","금").replace("Sat","토").replace("Sun","일")
    md  = datetime.now(KST).strftime("%m/%d")
    판   = "오전판" if datetime.now(KST).hour < 12 else "저녁판"
    subject = f"📰 사설브리핑 | {md} ({dow}) {판}"

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
  <p style="margin:0 0 14px;color:#666;font-size:13px;">✍️ {ed['author']}</p>
  <div style="font-size:15px;line-height:1.85;color:#333;
              border-top:1px solid #e8e8e8;padding-top:14px;">{paras}</div>
  <a href="{ed['url']}" style="display:inline-block;margin-top:10px;
     font-size:13px;color:#1a6ec8;">🔗 원문 보기</a>
</div>"""

    html = f"""<!DOCTYPE html><html lang="ko">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:'Malgun Gothic',sans-serif;max-width:700px;
             margin:0 auto;padding:20px;background:#fff;color:#222;">
  <div style="background:#1a3a5c;color:#fff;padding:20px 24px;
              border-radius:8px;margin-bottom:28px;">
    <div style="font-size:13px;opacity:.8;">{period}</div>
    <h1 style="margin:6px 0 0;font-size:22px;">📰 신문 사설 브리핑</h1>
    <div style="margin-top:6px;font-size:14px;opacity:.9;">{edition} · {date_str}</div>
  </div>
  <div style="background:#f0f4f8;border-left:4px solid #1a3a5c;
              padding:20px;border-radius:4px;margin-bottom:32px;">
    <h2 style="margin:0 0 14px;font-size:16px;color:#1a3a5c;">🤖 AI 요약 브리핑</h2>
    <div style="line-height:1.85;font-size:14px;">{summary_html}</div>
  </div>
  <h2 style="font-size:18px;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:8px;margin-bottom:20px;">📄 사설 원문</h2>
  {cards}
  <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
  <p style="color:#bbb;font-size:11px;text-align:center;">
    GitHub Actions + Gemini AI 자동 생성 | {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}
  </p>
</body></html>"""

    plain = f"[{edition}] {date_str} 사설 브리핑\n\n{summary}\n\n"
    for ed in editorials:
        plain += f"\n■ [{ed['paper']}] {ed['title']}\n작성자: {ed['author']}\n{ed['url']}\n\n{ed['content']}\n\n{'─'*40}\n"

    return subject, html, plain


def send_gmail(subject, html, plain):
    sender    = os.environ["SENDER_EMAIL"]
    password  = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
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

    print("② Gemini AI 요약 중...")
    summary = summarize(editorials, edition, start, end)
    print("   → 완료\n")

    print("③ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, summary, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
