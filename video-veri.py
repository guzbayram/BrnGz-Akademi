#!/usr/bin/env python3
"""MEBI ust konu sayfasindaki ders ve video verilerini JSON'a aktarir."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


MEBI_ORIGIN = "https://mebi.eba.gov.tr"
UPPER_SUBJECT_RE = re.compile(
    r"^https://mebi\.eba\.gov\.tr/student/section/upper-subjects/"
    r"[0-9a-fA-F-]+/?(?:\?.*)?$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MEBI ana/alt konu video baglantilarini JSON dosyasina aktarir."
    )
    parser.add_argument("url", help="MEBI upper-subjects baglantisi")
    parser.add_argument("-o", "--output", type=Path, help="Olusturulacak JSON dosyasi")
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path.home() / ".video-veri-browser",
        help="MEBI oturumunun saklanacagi Chrome profili",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Kayitli oturum varsa tarayiciyi gorunmeden calistir",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=300,
        help="Ilk giris icin beklenecek azami saniye (varsayilan: 300)",
    )
    parser.add_argument(
        "--page-timeout",
        type=int,
        default=45,
        help="Her sayfa icin beklenecek azami saniye (varsayilan: 45)",
    )
    return parser.parse_args()


def normalize_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def absolute_mebi_url(href: str) -> str:
    if href.startswith("/"):
        return f"{MEBI_ORIGIN}{href}"
    return href


def id_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def safe_filename(title: str) -> str:
    replacements = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    value = title.translate(replacements).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return f"mebi-{value or 'video'}-video-ders-link.json"


def chrome_driver(profile: Path, headless: bool) -> webdriver.Chrome:
    profile.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument(f"--user-data-dir={profile.expanduser().resolve()}")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1440,1000")
    if headless:
        options.add_argument("--headless=new")
    return webdriver.Chrome(options=options)


def links_containing(driver: webdriver.Chrome, path: str) -> list[dict[str, str]]:
    rows = driver.execute_script(
        r"""
        const path = arguments[0];
        const seen = new Set();
        return [...document.querySelectorAll('a[href]')]
          .map(a => ({
            href: a.href.split('#')[0].replace(/\/$/, ''),
            title: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim()
          }))
          .filter(x => x.href.includes(path) && !seen.has(x.href) && seen.add(x.href));
        """,
        path,
    )
    return [{"href": absolute_mebi_url(x["href"]), "title": x["title"]} for x in rows]


def wait_for_links(
    driver: webdriver.Chrome, path: str, timeout: int
) -> list[dict[str, str]]:
    WebDriverWait(driver, timeout).until(lambda d: links_containing(d, path))
    return links_containing(driver, path)


def wait_for_login(
    driver: webdriver.Chrome, source_url: str, login_timeout: int, page_timeout: int
) -> None:
    driver.get(source_url)
    try:
        wait_for_links(driver, "/student/section/lower-subjects/", page_timeout)
        return
    except TimeoutException:
        if "upper-subjects" in driver.current_url:
            raise

    print(
        "MEBI girisi gerekiyor. Acilan pencerede giris yapin; program otomatik devam edecek.",
        file=sys.stderr,
    )
    WebDriverWait(driver, login_timeout).until(
        lambda d: urlparse(d.current_url).path.startswith("/student/")
    )
    if normalize_url(driver.current_url) != source_url:
        driver.get(source_url)
    wait_for_links(driver, "/student/section/lower-subjects/", page_timeout)


def visible_heading(driver: webdriver.Chrome) -> str:
    script = r"""
      const selectors = ['h1', 'main h2', '.page-title', '.subject-title'];
      for (const selector of selectors) {
        for (const el of document.querySelectorAll(selector)) {
          const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
          if (text && el.getClientRects().length) return text;
        }
      }
      return document.title.replace(/\s*[-|]\s*MEB[Iİ].*$/i, '').trim();
    """
    return driver.execute_script(script) or "MEBI Video Dersleri"


def breadcrumb(driver: webdriver.Chrome, fallback_title: str) -> list[str]:
    values = driver.execute_script(
        r"""
        const root = document.querySelector('.breadcrumb, nav[aria-label*=breadcrumb i], .breadcrumbs');
        if (!root) return [];
        return [...root.querySelectorAll('a, li, span')]
          .map(x => (x.innerText || '').replace(/\s+/g, ' ').trim())
          .filter((x, i, a) => x && a.indexOf(x) === i);
        """
    )
    if values:
        return values
    return ["Giriş", fallback_title]


def video_data(driver: webdriver.Chrome, timeout: int) -> tuple[list[str], str | None]:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                "return document.readyState === 'complete' || !!document.querySelector('video, video source')"
            )
        )
    except TimeoutException:
        pass

    driver.execute_script(
        "document.querySelectorAll('video').forEach(v => { try { v.preload = 'metadata'; v.load(); } catch (_) {} });"
    )
    deadline = time.time() + min(timeout, 15)
    data = {"urls": [], "duration": None}
    while time.time() < deadline:
        data = driver.execute_script(
            r"""
            const urls = [];
            const add = value => {
              if (value && /^https?:/i.test(value) && !urls.includes(value)) urls.push(value);
            };
            document.querySelectorAll('video').forEach(v => {
              add(v.currentSrc); add(v.src);
              v.querySelectorAll('source[src]').forEach(s => add(s.src));
            });
            document.querySelectorAll('source[src]').forEach(s => add(s.src));
            performance.getEntriesByType('resource').forEach(r => {
              if (/\.(mp4|m3u8)(\?|$)/i.test(r.name)) add(r.name);
            });
            const video = [...document.querySelectorAll('video')]
              .find(v => Number.isFinite(v.duration) && v.duration > 0);
            const seconds = video ? Math.round(video.duration) : null;
            return {
              urls,
              duration: seconds === null ? null
                : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
            };
            """
        )
        if data["urls"] and data["duration"]:
            break
        time.sleep(0.5)
    return data["urls"], data["duration"]


def optional_quiz_links(driver: webdriver.Chrome) -> dict[str, str]:
    links = driver.execute_script(
        """
        return [...document.querySelectorAll('a[href*="/student/quiz/"]')]
          .map(a => ({href: a.href, text: (a.innerText || '').toLocaleLowerCase('tr-TR')}));
        """
    )
    result: dict[str, str] = {}
    for link in links:
        if "çıkmış" in link["text"] or "cikmis" in link["text"]:
            result.setdefault("pastQuestionsUrl", link["href"])
        elif "test" in link["text"] or "alıştır" in link["text"]:
            result.setdefault("practiceUrl", link["href"])
    return result


def scrape(args: argparse.Namespace) -> dict:
    source_url = normalize_url(args.url)
    driver = chrome_driver(args.profile, args.headless)
    driver.set_page_load_timeout(args.page_timeout)
    try:
        wait_for_login(
            driver, source_url, args.login_timeout, args.page_timeout
        )
        upper_title = visible_heading(driver)
        upper_breadcrumb = breadcrumb(driver, upper_title)
        lower_links = wait_for_links(
            driver, "/student/section/lower-subjects/", args.page_timeout
        )
        lower_subjects = []

        for lower_order, lower in enumerate(lower_links, 1):
            print(
                f"[{lower_order}/{len(lower_links)}] {lower['title'] or lower['href']}",
                file=sys.stderr,
            )
            driver.get(lower["href"])
            detail_links = wait_for_links(
                driver, "/student/section/subject-details/", args.page_timeout
            )
            lower_title = lower["title"] or visible_heading(driver)
            details = []

            for detail_order, detail in enumerate(detail_links, 1):
                driver.get(detail["href"])
                urls, duration = video_data(driver, args.page_timeout)
                title = detail["title"] or visible_heading(driver)
                item = {
                    "order": detail_order,
                    "id": id_from_url(detail["href"]),
                    "title": title,
                    "lessonUrl": detail["href"],
                }
                item.update(optional_quiz_links(driver))
                if urls:
                    item["videoUrl"] = urls[0]
                item["videoUrls"] = urls
                if duration:
                    item["duration"] = duration
                details.append(item)

            lower_subjects.append(
                {
                    "order": lower_order,
                    "id": id_from_url(lower["href"]),
                    "title": lower_title,
                    "lowerSubjectUrl": lower["href"],
                    "subjectDetailCount": len(details),
                    "videoCount": sum(bool(item["videoUrls"]) for item in details),
                    "subjectDetails": details,
                }
            )

        detail_count = sum(x["subjectDetailCount"] for x in lower_subjects)
        video_count = sum(x["videoCount"] for x in lower_subjects)
        return {
            "sourceUrl": source_url,
            "title": upper_title,
            "breadcrumb": upper_breadcrumb,
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "format": "upperSubject -> lowerSubjects[] -> subjectDetails[] with lessonUrl and videoUrl",
            "lowerSubjects": lower_subjects,
            "lowerSubjectCount": len(lower_subjects),
            "subjectDetailCount": detail_count,
            "videoCount": video_count,
            "missingVideoCount": detail_count - video_count,
        }
    finally:
        driver.quit()


def main() -> int:
    args = parse_args()
    if not UPPER_SUBJECT_RE.match(args.url):
        print("Hata: Gecerli bir MEBI upper-subjects baglantisi girin.", file=sys.stderr)
        return 2

    try:
        result = scrape(args)
    except (TimeoutException, WebDriverException) as error:
        print(f"Hata: MEBI verisi okunamadi: {error}", file=sys.stderr)
        return 1

    output = args.output or Path(safe_filename(result["title"]))
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{output}: {result['lowerSubjectCount']} ana konu, "
        f"{result['subjectDetailCount']} ders, {result['videoCount']} video",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
