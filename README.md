# 📰 신문 사설 자동 요약 봇

매일 아침 7시, 주요 신문 사설을 Claude AI가 자동으로 요약해 Gmail로 보내드립니다.

---

## 작동 방식

```
GitHub Actions (매일 7시)
    → RSS로 사설 수집 (조선·동아·한겨레·경향·중앙)
    → Claude AI로 주제 분류 + 요약
    → 광고 없는 텍스트 전용 원문 페이지 생성
    → Gmail로 발송
```

---

## 🛠 설정 방법 (딱 4단계)

### 1단계 — 이 저장소를 내 GitHub에 올리기

1. [github.com](https://github.com) 에서 **New repository** 클릭
2. 저장소 이름 입력 (예: `editorial-bot`)
3. `main.py`, `requirements.txt`, `.github/` 폴더를 업로드

> 📱 **모바일에서 하는 법**: GitHub 앱 → 저장소 선택 → 파일 업로드

---

### 2단계 — Gmail 앱 비밀번호 발급

일반 Gmail 비밀번호 대신 **앱 전용 비밀번호**가 필요합니다.

1. [myaccount.google.com](https://myaccount.google.com) 접속
2. **보안** → **2단계 인증** 활성화 (필수)
3. **보안** → **앱 비밀번호** 클릭
4. 앱: `메일`, 기기: `Windows 컴퓨터` 선택 → **생성**
5. 표시된 **16자리 코드** 복사 (예: `abcd efgh ijkl mnop`)

---

### 3단계 — GitHub Secrets 등록

GitHub 저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 이름 | 값 |
|-------------|-----|
| `ANTHROPIC_API_KEY` | Anthropic API 키 ([console.anthropic.com](https://console.anthropic.com)) |
| `SENDER_EMAIL` | 발송에 쓸 Gmail 주소 (예: `myemail@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 2단계에서 받은 16자리 앱 비밀번호 |
| `RECIPIENT_EMAIL` | 이메일 받을 주소 (발송자와 같아도 됨) |

---

### 4단계 — 테스트 실행

저장소 → **Actions** 탭 → **신문 사설 일일 요약** → **Run workflow** 클릭

수십 초 후 이메일이 도착하면 성공! 🎉  
이후로는 **매일 오전 7시에 자동 실행**됩니다.

---

## 💰 비용

| 항목 | 비용 |
|------|------|
| GitHub Actions | 무료 (월 2,000분 제공) |
| Claude API | 약 **1~3원/일** (claude-sonnet 기준) |
| Gmail | 무료 |

---

## ⚙️ 커스터마이징

### 신문사 추가/제거
`main.py` 상단 `RSS_FEEDS` 딕셔너리를 수정하세요.

```python
RSS_FEEDS = {
    "조선일보": "https://...",
    "동아일보": "https://...",
    # 원하는 신문사 추가
}
```

### 발송 시간 변경
`.github/workflows/daily_editorial.yml` 의 cron 값을 수정하세요.

```yaml
# KST 기준: UTC = KST - 9시간
- cron: '0 22 * * *'   # 오전 7시 KST
- cron: '0 21 * * *'   # 오전 6시 KST
- cron: '30 22 * * *'  # 오전 7시 30분 KST
```

---

## ❓ 자주 묻는 질문

**Q. Actions가 실행됐는데 이메일이 안 와요**  
→ Actions 로그에서 오류 확인. Gmail 앱 비밀번호와 2단계 인증을 다시 확인하세요.

**Q. 사설이 0개 수집됐어요**  
→ RSS 주소가 변경됐을 수 있습니다. 각 신문사 사이트에서 최신 RSS 링크를 확인하세요.

**Q. 카카오톡으로도 받고 싶어요**  
→ 카카오 알림톡 API 연동이 필요합니다. 별도 설정이 필요하니 문의해 주세요.
