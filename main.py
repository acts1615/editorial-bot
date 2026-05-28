"""
신문 사설 자동 요약 & 이메일 발송 봇 v6
구조: 신문사/제목/작성자 → AI 요약 → 원문 (신문사별 개별 구성)
"""

import os, re, smtplib, json
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

BLOCKED_DOMAINS = ["nongaek.com", "newsis.com", "news1.kr", "yna.co.kr"]

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


def search_naver_editorial(paper):
    client_id     = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    params  = {"query": f"{paper} 사설", "display": 50, "sort": "date"}
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    try:
        resp  = requests.get("https://openapi.naver.com/v1/search/news.json",
                             headers=headers, params=params, timeout=10)
        print(f"    네이버 API: {resp.status_code}, {len(resp.json().get('items',[]))}개")
        for item in resp.json().get("items", []):
            title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            link  = item.get("originallink") or item.get("link", "")
            pub   = item.get("pubDate", "")

            if any(b in link for b in BLOCKED_DOMAINS):
                continue

            editorial_url = any(p in link for p in [
                "/opinion/editorial", "/arti/opinion/editorial",
                "/Opinion/article", "/news/Opinion", "/editorial/"
            ])
            editorial_title = title.startswith("[사설]") or title.startswith("사설")
            if not editorial_title and not editorial_url:
                continue
            if any(x in title for x in ["[단독]", "[인터뷰]", "학위복", "[속보]"]):
                continue

            try:
                pub_dt  = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                if (datetime.now(KST) - pub_dt).total_seconds() / 3600 > 48:
                    continue
            except:
                pub_str = pub[:16] if pub else "시각 미상"

            scraped = scrape_article(link, paper) if link else {}
            content = scraped.get("content", "")
            author  = scraped.get("author", "") or "논설위원실"

            ui_noise = ["공유하기", "카카오톡으로 공유하기", "페이스북으로 공유하기",
                        "트위터로 공유하기", "URL 복사", "창 닫기", "SNS", "퍼가기"]
            lines   = [l for l in content.split("\n") if not any(n in l for n in ui_noise)]
            content = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

            if len(content) < 100:
                content = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()

            if title and content:
                print(f"    ✓ {title[:40]}")
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
    keywords = ["전쟁", "분쟁 교전", "북한", "원유 에너지 안보", "해협", "핵 미사일"]
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


def build_email(editorials, sisain, security_news, summaries, edition, start, end):
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

    # 사설 (신문사별: 헤더 → AI요약 → 원문)
    editorial_blocks = ""
    for ed in editorials:
        ai = summaries.get(ed["paper"], {})
        ai_summary  = ai.get("summary", "요약을 불러올 수 없습니다.")
        stance      = ai.get("stance", "")
        keywords    = " ".join([f"#{k}" for k in ai.get("keywords", [])])

        paras = "".join(
            f"<p style='margin:0 0 14px;font-size:15px;line-height:1.85;color:#1a1a1a;text-indent:1em;'>{p}</p>"
            for p in ed["content"].split("\n") if p.strip() and len(p.strip()) > 10
        )

        editorial_blocks += f"""
<div style="margin-bottom:40px;border-radius:12px;overflow:hidden;
            box-shadow:0 2px 16px rgba(0,0,0,0.10);">

  <!-- ① 헤더: 신문사 / 제목 / 작성자 -->
  <div style="background:#1a3a5c;padding:18px 22px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
      <span style="background:rgba(255,255,255,0.2);color:#fff;font-size:13px;
                   font-weight:bold;padding:3px 12px;border-radius:20px;">{ed['paper']}</span>
      <span style="color:rgba(255,255,255,0.65);font-size:12px;">{ed['pub']}</span>
    </div>
    <h2 style="margin:0 0 8px;font-size:18px;color:#fff;line-height:1.4;font-weight:bold;">
      {ed['title']}
    </h2>
    <span style="color:rgba(255,255,255,0.7);font-size:13px;">✍️ {ed['author']}</span>
  </div>

  <!-- ② AI 요약 -->
  <div style="background:#eef2f7;padding:18px 22px;border-bottom:1px solid #d0d8e4;">
    <div style="font-size:12px;color:#1a3a5c;font-weight:bold;margin-bottom:8px;">🤖 AI 요약</div>
    <p style="margin:0 0 10px;font-size:14px;line-height:1.8;color:#333;">{ai_summary}</p>
    <div style="font-size:12px;color:#666;">
      논조: <strong>{stance}</strong> &nbsp;·&nbsp; {keywords}
    </div>
  </div>

  <!-- ③ 원문 -->
  <div style="background:#fffef9;padding:22px 26px;">
    <div style="font-size:12px;color:#999;margin-bottom:14px;font-weight:bold;">📄 원문</div>
    {paras}
    <div style="border-top:1px solid #eee;padding-top:12px;margin-top:4px;">
      <a href="{ed['url']}" style="font-size:13px;color:#888;text-decoration:none;">
        🔗 광고 없이 원문 보기
      </a>
    </div>
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

  {sisain_html}

  <!-- 사설 섹션 -->
  <h2 style="font-size:18px;color:#1a3a5c;border-bottom:2px solid #1a3a5c;
             padding-bottom:8px;margin:28px 0 20px;">📰 오늘의 사설</h2>
  {editorial_blocks}

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
        plain += f"[원문]\n{ed['content']}\n🔗 {ed['url']}\n"

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

    print("② 북한/전쟁 뉴스 수집 중...")
    security_news = get_security_news()
    print()

    print("③ 시사인 새로 나온 책 수집 중...")
    sisain = get_sisain_books()
    print()

    print("④ 사설별 AI 요약 중...")
    summaries = summarize_each(editorials)
    print()

    print("⑤ 이메일 발송 중...")
    subject, html, plain = build_email(editorials, sisain, security_news, summaries, edition, start, end)
    send_gmail(subject, html, plain)
    print("\n🎉 완료!")
