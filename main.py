"""
신문 사설 자동 요약 & 이메일 발송 봇 v2
- 원문 + 작성자 + 신문사 포함
- 아침 7시 (전날 18:50 ~ 당일 06:59) / 저녁 7시 (당일 07:00 ~ 18:50) 2회 배달
"""

import os, re, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import google.generativeai as genai
import feedparser
import requests
from bs4 import BeautifulSoup

# ── 시간 설정 ─────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def get_time_window() -> tuple[datetime, datetime, str]:
    """현재 시각 기준으로 수집 시간 범위와 에디션 이름을 반환합니다."""
    now = datetime.now(KST)
    hour = now.hour

    if hour < 12:
        # 아침판: 전날 18:50 ~ 오늘 06:59
        start = (now - timedelta(days=1)).replace(hour=18, minute=50, second=0, microsecond=0)
        end   = now.replace(hour=6, minute=59, second=59, microsecond=0)
        edition = "🌅 아침판"
    else:
        # 저녁판: 오늘 07:00 ~ 18:50
        start = now.replace(hour=7, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=18, minute=50, second=59, microsecond=0)
        edition = "🌆 저녁판"

    return start, end, edition

# ── RSS 피드 목록 ─────────────────────────────────────────────
RSS_FEEDS = {
    "조선일보": "https://www.chosun.com/arc/outboundfeeds/rss/category/opinion/editorial/",
    "동아일보": "https://rss.donga.com/editorial.xml",
    "한겨레":   "https://www.hani.co.kr/rss/opinion/editorial/",
    "경향신문": "https://www.khan.co.kr/rss/rssdata/opinion_editorial.xml",
    "중앙일보": "https://rss.joins.com/joins_news_list.xml",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# 신문사별 본문 / 작성자 CSS 셀렉터
PAPER_CONFIG = {
    "조선일보": {
        "body":   [".article-body", "#fusion-app article"],
        "author": [".article__author-name", ".byline"],
    },
    "동아일보": {
        "body":   [".article_txt", "#contents .news_view"],
        "author": [".reporter_name", ".article_info .name"],
    },
    "한겨레": {
        "body":   [".article-text", ".text"],
        "author": [".byline strong", ".reporter-name"],
    },
    "경향신문": {
        "body":   [".art_body", ".news_view_text"],
        "author": [".reporter_area .name", ".byline"],
    },
    "중앙일보": {
        "body":   [".article_body", "#article_body"],
        "author": [".byline__name", ".reporter-name"],
    },
}
DEFAULT_BODY_SEL   = ["article", ".article", ".news_body", "#articleBody", "main"]
DEFAULT_AUTHOR_SEL = [".author", ".byline", ".reporter", "[rel='author']"]


# ── 1. 웹 크롤링 ─────────────────────────────────────────────
def scrape_article(url: str, paper: str) -> dict:
    """URL에서 본문과 작성자를 추출합니다."""
    result = {"content": "", "author": ""}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")

        cfg = PAPER_CONFIG.get(paper, {})

        # 본문 추출
        for sel in cfg.get("body", []) + DEFAULT_BODY_SEL:
            tag = soup.select_one(sel)
            if tag:
                text = tag.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    result["content"] = text
                    break

        # 작성자 추출
        for sel in cfg.get("author", []) + DEFAULT_AUTHOR_SEL:
            tag = soup.select_one(sel)
            if tag:
                author = tag.get_text(strip=True)
                if author and len(author) < 30:
                    result["author"] = author
                    break

    except Exception as e:
        print(f"  크롤링 실패 ({paper}): {e}")
    return result


# ── 2. 사설 수집 ─────────────────────────────────────────────
def get_editorials(start: datetime, end: datetime) -> list[dict]:
    """시간 범위 내 사설을 수집합니다."""
    editorials = []

    for paper, feed_url in RSS_FEEDS.items():
        print(f"  [{paper}] 수집 중...")
        try:
            feed = feedparser.parse(feed_url, request_headers=HEADERS)
            for entry in feed.entries:

                # ── 발행 시각 파싱 ──
                pub_dt = None
                for attr in ("published_parsed", "updated_parsed"):
                    if getattr(entry, attr, None):
                        t = getattr(entry, attr)
                        pub_dt = datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST)
                        break

                # 날짜 필터: 48시간 이내만 수집
                if pub_dt and (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 48:
                    continue

                title  = entry.get("title", "").strip()
                link   = entry.get("link", "")
                rss_summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "").strip()

                # RSS 작성자
                rss_author = entry.get("author", "").strip()

                # 본문 크롤링
                scraped = scrape_article(link, paper) if link else {}
                content = scraped.get("content") or rss_summary
                author  = scraped.get("author") or rss_author or "논설위원실"

                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "시각 미상"

                if title and content:
                    editorials.append({
                        "paper":   paper,
                        "title":   title,
                        "author":  author,
                        "pub":     pub_str,
                        "content": content,
                        "url":     link,
                    })
                    print(f"    ✓ [{pub_str}] {title[:35]}...")

        except Exception as e:
            print(f"  [{paper}] 오류: {e}")

    return editorials


# ── 3. Claude AI 요약 ─────────────────────────────────────────
def summarize_with_gemini(editorials: list[dict], edition: str, start: datetime, end: datetime) -> str:
    """Gemini API로 사설을 주제별 분류 및 요약합니다. (완전 무료)"""
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.0-flash")

    if not editorials:
        return "수집된 사설이 없습니다."

    period = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    corpus = ""
    for ed in editorials:
        corpus += (
            f"\n\n【{ed['paper']}】 {ed['title']}\n"
            f"작성자: {ed['author']} | 발행: {ed['pub']}\n"
            f"{ed['content'][:2500]}\n"
            f"URL: {ed['url']}"
        )

    prompt = f"""다음은 {period} 주요 신문 사설들입니다.

{corpus}

아래 형식으로 정리해 주세요.

━━━━━━━━━━━━━━━━━━━━━━
📌 핵심 이슈 총평 (3줄 이내)
━━━━━━━━━━━━━━━━━━━━━━
(이 시간대 사설들의 공통 화두를 간결하게)

━━━━━━━━━━━━━━━━━━━━━━
🗂 주제별 분류
━━━━━━━━━━━━━━━━━━━━━━
카테고리: [정치/외교] [경제/산업] [사회/교육] [국제/안보] [기타]
형식: ● [카테고리] 신문사 — 제목

━━━━━━━━━━━━━━━━━━━━━━
📰 사설별 요약
━━━━━━━━━━━━━━━━━━━━━━
각 사설마다:
▶ [신문사] 제목
  • 주요 주장: (2문장)
  • 논조: 진보/보수/중도
  • 키워드: #태그 #태그 #태그

한국어로만 작성하고, 5분 안에 읽을 수 있게 간결하게 써 주세요."""

    response = model.generate_content(prompt)
    return response.text


# ── 4. HTML 이메일 생성 ───────────────────────────────────────
def build_html(editorials: list[dict], summary: str, edition: str,
               start: datetime, end: datetime) -> tuple[str, str]:
    """HTML 이메일 본문과 plain text를 반환합니다."""

    period    = f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
    date_str  = datetime.now(KST).strftime("%Y년 %m월 %d일")
    dow = datetime.now(KST).strftime("%a").replace(
        "Mon","월").replace("Tue","화").replace("Wed","수").replace(
        "Thu","목").replace("Fri","금").replace("Sat","토").replace("Sun","일")
    md  = datetime.now(KST).strftime("%m/%d")
    판   = "오전판" if datetime.now(KST).hour < 12 else "저녁판"
    subject = f"📰 사설브리핑 | {md} ({dow}) {판}"

    # ── 요약 HTML ──
    summary_html = summary.replace("━━━━━━━━━━━━━━━━━━━━━━", "<hr style='border:1px solid #ddd;'>")
    summary_html = re.sub(r"📌", "📌", summary_html)
    summary_html = summary_html.replace("\n", "<br>")

    # ── 원문 카드 HTML ──
    cards_html = ""
    for ed in editorials:
        body_paragraphs = ""
        for para in ed["content"].split("\n"):
            para = para.strip()
            if para:
                body_paragraphs += f"<p style='margin:0 0 12px;'>{para}</p>"

        cards_html += f"""
<div style="border:1px solid #e0e0e0;border-radius:8px;padding:20px;margin-bottom:24px;background:#fafafa;">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">
    <span style="background:#1a3a5c;color:#fff;font-size:12px;font-weight:bold;
                 padding:3px 10px;border-radius:20px;">{ed['paper']}</span>
    <span style="color:#888;font-size:12px;">{ed['pub']}</span>
  </div>
  <h3 style="margin:0 0 6px;font-size:17px;color:#1a1a1a;">{ed['title']}</h3>
  <p style="margin:0 0 14px;color:#666;font-size:13px;">✍️ {ed['author']}</p>
  <div style="font-size:15px;line-height:1.85;color:#333;border-top:1px solid #e8e8e8;padding-top:14px;">
    {body_paragraphs}
  </div>
  <a href="{ed['url']}" style="display:inline-block;margin-top:10px;font-size:13px;
     color:#1a6ec8;text-decoration:none;">🔗 원문 보기</a>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:'Malgun Gothic',Apple SD Gothic Neo,sans-serif;
             max-width:700px;margin:0 auto;padding:20px;color:#222;background:#fff;">

  <!-- 헤더 -->
  <div style="background:#1a3a5c;color:#fff;padding:20px 24px;border-radius:8px;margin-bottom:28px;">
    <div style="font-size:13px;opacity:.8;">{period}</div>
    <h1 style="margin:6px 0 0;font-size:22px;">📰 신문 사설 브리핑</h1>
    <div style="margin-top:6px;font-size:14px;opacity:.9;">{edition} · {date_str}</div>
  </div>

  <!-- AI 요약 -->
  <div style="background:#f0f4f8;border-left:4px solid #1a3a5c;
              padding:20px;border-radius:4px;margin-bottom:32px;">
    <h2 style="margin:0 0 14px;font-size:16px;color:#1a3a5c;">🤖 AI 요약 브리핑</h2>
    <div style="line-height:1.85;font-size:14px;">{summary_html}</div>
  </div>

  <!-- 원문 섹션 -->
  <h2 style="font-size:18px;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:8px;margin-bottom:20px;">📄 사설 원문</h2>
  {cards_html}

  <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
  <p style="color:#bbb;font-size:11px;text-align:center;">
    GitHub Actions + Claude AI 자동 생성 | {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}
  </p>
</body>
</html>"""

    # plain text
    plain = f"[{edition}] {date_str} 신문 사설 브리핑 ({period})\n\n"
    plain += "=" * 50 + "\n[AI 요약]\n" + "=" * 50 + "\n"
    plain += summary + "\n\n"
    plain += "=" * 50 + "\n[원문]\n" + "=" * 50 + "\n"
    for ed in editorials:
        plain += f"\n■ [{ed['paper']}] {ed['title']}\n"
        plain += f"  작성자: {ed['author']} | 발행: {ed['pub']}\n"
        plain += f"  URL: {ed['url']}\n\n"
        plain += ed["content"] + "\n\n"
        plain += "-" * 40 + "\n"

    return subject, html, plain


# ── 5. Gmail 발송 ─────────────────────────────────────────────
def send_gmail(subject: str, html: str, plain: str) -> None:
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


# ── 메인 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    now = datetime.now(KST)
    print(f"\n{'='*55}")
    print(f"📰 신문 사설 봇 시작: {now.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*55}\n")

    start, end, edition = get_time_window()
    print(f"📅 수집 범위: {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')} ({edition})\n")

    print("① 사설 수집 중...")
    editorials = get_editorials(start, end)
    print(f"   → {len(editorials)}개 수집 완료\n")

    print("② Claude로 요약 중...")
    summary = summarize_with_gemini(editorials, edition, start, end)
    print("   → 완료\n")

    print("③ 이메일 생성 및 발송 중...")
    subject, html, plain = build_html(editorials, summary, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
