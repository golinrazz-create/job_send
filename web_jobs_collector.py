import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ================= НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ =================
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", EMAIL_SENDER)

HISTORY_FILE = os.getenv("WEB_HISTORY_FILE", "web_seen_history.json")
MAX_HISTORY_ITEMS = 500

# Профильные ключевые слова для фильтрации ленты VC.ru (чтобы отсечь нерелевантные роли)
VC_KEYWORDS = (
    "редактор",
    "главред",
    "шеф-редактор",
    "контент",
    "медиа",
    "автор",
    "копирайтер",
    "журналист",
    "редакц",
    "smm",
    "смм",
    "ai",
    "ии",
    "нейросет",
    "бренд-медиа",
    "editorial",
    "content",
)

# Разделы Kadrof.ru: все вакансии + при необходимости можно сузить до /work/cat15 (тексты) и /work/cat16 (маркетинг)
KADROF_URLS = [
    "https://www.kadrof.ru/work",
]

VC_HR_URL = "https://vc.ru/hr"
VC_API_URL = "https://api.vc.ru/v2.5/timeline?subsitesIds=220780&sorting=new"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
# ============================================================


def load_history() -> Dict[str, List[str]]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "kadrof": list(data.get("kadrof", [])),
                    "vc": list(data.get("vc", [])),
                }
        except Exception as e:
            print(f"[History Load Warning]: {e}")
    return {"kadrof": [], "vc": []}


def save_history(history: Dict[str, List[str]]) -> None:
    history["kadrof"] = history.get("kadrof", [])[-MAX_HISTORY_ITEMS:]
    history["vc"] = history.get("vc", [])[-MAX_HISTORY_ITEMS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def matches_profile(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in VC_KEYWORDS)


def fetch_kadrof(seen_ids: set) -> Tuple[List[Dict], str]:
    """Собирает свежие объявления с Kadrof.ru через requests + BeautifulSoup."""
    found: List[Dict] = []
    local_seen = set()

    try:
        for base_url in KADROF_URLS:
            resp = requests.get(base_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # На Kadrof карточки вакансий ведут на /work/<id> (цифровой ID)
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                match = re.search(r"/work/(\d+)", href)
                if not match:
                    continue

                item_id = match.group(1)
                if item_id in seen_ids or item_id in local_seen:
                    continue

                title = " ".join(a_tag.stripped_strings).strip()
                if len(title) < 6:
                    continue

                full_url = urljoin("https://www.kadrof.ru", href)

                # Пробуем вытащить краткий контекст из родительского блока карточки
                parent = a_tag.find_parent(["div", "article", "li", "tr"])
                snippet = ""
                if parent:
                    raw_block = " ".join(parent.stripped_strings)
                    snippet = raw_block.replace(title, "", 1).strip()[:280]

                local_seen.add(item_id)
                found.append(
                    {
                        "id": item_id,
                        "source": "Kadrof.ru",
                        "title": title,
                        "url": full_url,
                        "snippet": snippet,
                    }
                )

        return found, f"OK (+{len(found)})"
    except Exception as e:
        return [], f"Ошибка: {e}"


def _parse_vc_html(html: str, seen_ids: set) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: List[Dict] = []
    local_seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        match = re.search(r"/hr/(\d+)", href)
        if not match:
            continue

        item_id = match.group(1)
        if item_id in seen_ids or item_id in local_seen:
            continue

        title = " ".join(a_tag.stripped_strings).strip()
        if len(title) < 8:
            continue

        parent = a_tag.find_parent(["article", "div"])
        block_text = " ".join(parent.stripped_strings) if parent else title
        if not matches_profile(block_text):
            continue

        full_url = urljoin("https://vc.ru", href.split("?")[0])
        snippet = block_text.replace(title, "", 1).strip()[:300]

        local_seen.add(item_id)
        found.append(
            {
                "id": item_id,
                "source": "VC.ru/hr",
                "title": title,
                "url": full_url,
                "snippet": snippet,
            }
        )
    return found


def fetch_vc(seen_ids: set) -> Tuple[List[Dict], str]:
    """
    Собирает вакансии из раздела VC.ru/hr:
    1) Пробует лёгкий HTTP-запрос (API/HTML).
    2) Если страница отдала 0 карточек из-за JS/антибота и установлен Playwright — использует его как резерв.
    """
    # Попытка 1: лёгкий HTML-запрос
    try:
        resp = requests.get(VC_HR_URL, headers=HEADERS, timeout=20)
        if resp.status_code == 200 and "/hr/" in resp.text:
            items = _parse_vc_html(resp.text, seen_ids)
            if items:
                return items, f"OK via HTTP (+{len(items)})"
    except Exception as http_err:
        print(f"[VC HTTP info]: {http_err}")

    # Попытка 2: внутренний JSON-таймлайн
    try:
        api_resp = requests.get(VC_API_URL, headers=HEADERS, timeout=20)
        if api_resp.status_code == 200:
            payload = api_resp.json()
            raw_items = payload.get("result", {}).get("items", [])
            found: List[Dict] = []
            for entry in raw_items:
                data = entry.get("data", entry)
                item_id = str(data.get("id", ""))
                if not item_id or item_id in seen_ids:
                    continue
                title = (data.get("title") or "").strip()
                intro = (data.get("intro") or "").strip()
                combined = f"{title} {intro}".strip()
                if not combined or not matches_profile(combined):
                    continue
                url = data.get("url") or f"https://vc.ru/hr/{item_id}"
                found.append(
                    {
                        "id": item_id,
                        "source": "VC.ru/hr",
                        "title": title or intro[:90],
                        "url": url,
                        "snippet": intro[:300],
                    }
                )
            if found or raw_items:
                return found, f"OK via API (+{len(found)})"
    except Exception as api_err:
        print(f"[VC API info]: {api_err}")

    # Попытка 3: резервный headless-браузер Playwright (если установлен в окружении)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(VC_HR_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()

        items = _parse_vc_html(html, seen_ids)
        return items, f"OK via Playwright (+{len(items)})"
    except ImportError:
        return [], "Пусто/JS-защита (Playwright не установлен)"
    except Exception as pw_err:
        return [], f"Ошибка: {pw_err}"


def build_email_bodies(items: List[Dict], statuses: Dict[str, str]) -> Tuple[str, str]:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Plain-text версия (удобна для автоматического парсинга основным пайплайном)
    text_lines = [
        f"WEB-JOBS DIGEST ({now_str})",
        f"Всего новых позиций: {len(items)}",
        "=" * 50,
        "",
    ]
    for idx, item in enumerate(items, 1):
        text_lines.append(f"{idx}. [{item['source']}] {item['title']}")
        text_lines.append(f"Ссылка: {item['url']}")
        if item.get("snippet"):
            text_lines.append(f"Описание: {item['snippet']}")
        text_lines.append("-" * 40)

    text_lines.append("")
    text_lines.append("Статус обхода источников:")
    for src, st in statuses.items():
        text_lines.append(f"- {src}: {st}")

    # HTML версия (удобна для чтения глазами в Gmail)
    html_parts = [
        "<html><body style='font-family: Arial, sans-serif; color: #222;'>",
        f"<h2>Web-Jobs Digest: найдено {len(items)} новых позиций</h2>",
        f"<p style='color: #666; font-size: 13px;'>Сборка от {now_str}</p><hr>",
    ]
    for item in items:
        snippet_html = (
            f"<div style='color: #444; margin-top: 4px;'>{item['snippet']}</div>"
            if item.get("snippet")
            else ""
        )
        html_parts.append(
            f"<div style='margin-bottom: 16px;'>"
            f"<span style='background:#eef2ff; padding:2px 6px; border-radius:4px; font-size:12px;'>"
            f"{item['source']}</span> "
            f"<a href='{item['url']}' style='font-weight:bold; font-size:15px;'>{item['title']}</a>"
            f"{snippet_html}"
            f"<div style='font-size:12px; color:#888; margin-top:2px;'>{item['url']}</div>"
            f"</div>"
        )

    html_parts.append("<hr><p style='font-size: 12px; color: #666;'><b>Статус обхода:</b><br>")
    for src, st in statuses.items():
        html_parts.append(f"{src}: {st}<br>")
    html_parts.append("</p></body></html>")

    return "\n".join(text_lines), "".join(html_parts)


def send_digest_email(items: List[Dict], statuses: Dict[str, str]) -> bool:
    if not items:
        print("Новых вакансий не найдено. Письмо не отправляется.")
        return True

    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print("[Email Error]: Не заданы EMAIL_SENDER или EMAIL_PASSWORD в переменных окружения.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Web-Jobs] Вакансии: VC.ru и Kadrof ({len(items)})"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    plain_body, html_body = build_email_bodies(items, statuses)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        print(f"Письмо успешно отправлено на {EMAIL_RECEIVER} ({len(items)} позиций).")
        return True
    except Exception as e:
        print(f"[Email Error]: {e}")
        return False


def main() -> None:
    history = load_history()
    kadrof_seen = set(history.get("kadrof", []))
    vc_seen = set(history.get("vc", []))

    kadrof_items, kadrof_status = fetch_kadrof(kadrof_seen)
    vc_items, vc_status = fetch_vc(vc_seen)

    statuses = {
        "Kadrof.ru": kadrof_status,
        "VC.ru/hr": vc_status,
    }
    print("Статус обхода:", statuses)

    all_items = kadrof_items + vc_items
    sent_ok = send_digest_email(all_items, statuses)

    # Обновляем историю только если письмо реально ушло (или если новых позиций не было)
    if sent_ok and all_items:
        history["kadrof"].extend(item["id"] for item in kadrof_items)
        history["vc"].extend(item["id"] for item in vc_items)
        save_history(history)


if __name__ == "__main__":
    main()
