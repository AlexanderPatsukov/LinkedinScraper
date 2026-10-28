"""HTTP-клиент и парсер публичных (гостевых) страниц вакансий LinkedIn.

Используются только эндпоинты, которые отдаются БЕЗ авторизации - никаких
кук аккаунта. Это важно: автоматические запросы из-под залогиненного
аккаунта LinkedIn приводят к его блокировке, а гостевые страницы с профилем
пользователя никак не связаны.

    /jobs-guest/jobs/api/seeMoreJobPostings/search  -> HTML-список карточек (10 шт.)
    /jobs-guest/jobs/api/jobPosting/{job_id}        -> HTML с полным описанием
    /company/{slug}/                                -> HTML профиля компании

Структура разметки проверена на живых ответах (октябрь 2026).
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
COMPANY_URL = "https://www.linkedin.com/company/{slug}/"

RESULTS_PER_PAGE = 10
# Жёсткий потолок LinkedIn: start=0..990 отдаёт данные, start=1000 - HTTP 400.
# Ровно 1000 результатов на одну комбинацию параметров поиска.
HARD_PAGE_CAP = 100

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}


class ScraperError(RuntimeError):
    """Запрос не удался после всех повторов."""


class RateLimited(ScraperError):
    """LinkedIn ответил 429 - надо притормозить."""


class Blocked(ScraperError):
    """999 / authwall - доступ прикрыт."""


def _clean(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _company_slug(url: str | None) -> str:
    """https://ae.linkedin.com/company/ajax-systems?trk=... -> ajax-systems"""
    if not url:
        return ""
    match = re.search(r"/company/([^/?#]+)", url)
    if not match:
        return ""
    return match.group(1).strip().lower()


def _canonical_company_url(url: str | None) -> str:
    slug = _company_slug(url)
    return COMPANY_URL.format(slug=slug) if slug else ""


@dataclass
class JobCard:
    job_id: str
    title: str = ""
    company_name: str = ""
    company_slug: str = ""
    company_url: str = ""
    location: str = ""
    url: str = ""
    posted_raw: str = ""
    posted_at: date | None = None
    is_reposted: bool = False
    benefits: str = ""


@dataclass
class JobDetail:
    description: str = ""
    description_html: str = ""
    applicants_raw: str = ""
    seniority: str = ""
    employment_type: str = ""
    job_function: str = ""
    industries: str = ""
    company_url: str = ""
    company_name: str = ""
    location: str = ""
    criteria: dict = field(default_factory=dict)


@dataclass
class CompanyProfile:
    name: str = ""
    description: str = ""
    tagline: str = ""
    industry: str = ""
    company_size: str = ""
    headquarters: str = ""
    website: str = ""
    followers: str = ""
    logo_url: str = ""


class LinkedInGuestClient:
    """Сессия с повторами, бэкоффом и паузой между запросами."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: int = 20,
        delay: float = 2.0,
        max_retries: int = 3,
    ):
        self.timeout = timeout
        self.delay = max(delay, 0.0)
        self.max_retries = max(max_retries, 1)
        self.session = requests.Session()
        self.session.headers.update({**DEFAULT_HEADERS, "user-agent": user_agent})
        self._last_request_at = 0.0

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "LinkedInGuestClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _throttle(self) -> None:
        if not self.delay:
            return
        elapsed = time.monotonic() - self._last_request_at
        # небольшой джиттер, чтобы запросы не шли строго по метроному
        wait = self.delay + random.uniform(0, self.delay * 0.25) - elapsed
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, params: dict | None = None, *, retries: int | None = None) -> str:
        last_error: Exception | None = None
        attempts = self.max_retries if retries is None else max(retries, 1)
        for attempt in range(1, attempts + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Сетевая ошибка (%s/%s) %s: %s", attempt, self.max_retries, url, exc)
                time.sleep(min(2**attempt, 30))
                continue

            status = response.status_code
            if status == 200:
                return response.text
            if status in (404, 410):
                raise ScraperError(f"HTTP {status}: вакансия или страница удалена")
            if status == 999:
                # 999 - временный рейт-лимит, а не запрет на конкретную страницу:
                # после паузы те же URL снова отдают 200. Поэтому ретраим с
                # длинным бэкоффом, а не падаем сразу.
                last_error = Blocked(f"HTTP 999: LinkedIn придержал доступ (попыток: {attempt})")
                if attempt >= attempts:
                    break
                pause = min(10 * attempt, 60)
                logger.warning("HTTP 999, пауза %s сек (%s/%s)", pause, attempt, attempts)
                time.sleep(pause)
                continue
            if status == 429:
                retry_after = response.headers.get("retry-after")
                pause = int(retry_after) if (retry_after or "").isdigit() else min(15 * attempt, 90)
                logger.warning("HTTP 429, пауза %s сек (%s/%s)", pause, attempt, self.max_retries)
                last_error = RateLimited(f"HTTP 429 после {attempt} попыток")
                time.sleep(pause)
                continue
            if 500 <= status < 600:
                last_error = ScraperError(f"HTTP {status}")
                time.sleep(min(2**attempt, 30))
                continue
            raise ScraperError(f"HTTP {status}")

        if isinstance(last_error, (RateLimited, Blocked)):
            raise last_error
        raise ScraperError(f"Запрос не удался после {self.max_retries} попыток: {last_error}")

    # --- высокоуровневые методы ---

    def search_page(self, params: dict) -> list[JobCard]:
        html = self.get(SEARCH_URL, params=params)
        return parse_search_cards(html)

    def job_detail(self, job_id: str) -> JobDetail:
        html = self.get(JOB_URL.format(job_id=job_id))
        return parse_job_detail(html)

    def company_profile(self, slug: str) -> CompanyProfile:
        # Одна попытка: страницы компаний лимитируются чаще всего, и долгий
        # бэкофф на каждую застопорил бы весь сбор. Блокировки считает вызывающий
        # код, а непрогруженное подберёт «Догрузить недостающее».
        html = self.get(COMPANY_URL.format(slug=slug), retries=1)
        return parse_company_profile(html)


# ---------------------------------------------------------------- parsing

def parse_search_cards(html: str) -> list[JobCard]:
    soup = BeautifulSoup(html, "lxml")
    cards: list[JobCard] = []

    for node in soup.select("[data-entity-urn]"):
        urn = node.get("data-entity-urn", "")
        match = re.search(r"jobPosting:(\d+)", urn)
        if not match:
            continue
        card = JobCard(job_id=match.group(1))

        title_el = node.select_one("h3.base-search-card__title") or node.select_one(".sr-only")
        card.title = _clean(title_el.get_text() if title_el else "")

        company_el = node.select_one("h4.base-search-card__subtitle a") or node.select_one(
            "h4.base-search-card__subtitle"
        )
        card.company_name = _clean(company_el.get_text() if company_el else "")
        company_href = company_el.get("href") if company_el and company_el.has_attr("href") else ""
        card.company_slug = _company_slug(company_href)
        card.company_url = _canonical_company_url(company_href)

        location_el = node.select_one(".job-search-card__location")
        card.location = _clean(location_el.get_text() if location_el else "")

        link_el = node.select_one("a.base-card__full-link")
        href = link_el.get("href", "") if link_el else ""
        card.url = href.split("?")[0] if href else f"https://www.linkedin.com/jobs/view/{card.job_id}/"

        time_el = node.select_one("time")
        if time_el:
            card.posted_raw = _clean(time_el.get_text())
            raw_date = time_el.get("datetime", "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date or ""):
                card.posted_at = date.fromisoformat(raw_date)
        # «Reposted 1 week ago» / «Вакансия размещена повторно»
        card.is_reposted = "repost" in card.posted_raw.lower() or "повторно" in card.posted_raw.lower()

        benefit_el = node.select_one(".job-posting-benefits__text") or node.select_one(".result-benefits__text")
        card.benefits = _clean(benefit_el.get_text() if benefit_el else "")

        cards.append(card)

    return cards


def parse_job_detail(html: str) -> JobDetail:
    soup = BeautifulSoup(html, "lxml")
    detail = JobDetail()

    markup = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    if markup:
        detail.description_html = markup.decode_contents().strip()
        detail.description = markup.get_text("\n", strip=True)

    title_el = soup.select_one(".top-card-layout__title") or soup.select_one(".topcard__title")
    if title_el:
        detail.criteria["title"] = _clean(title_el.get_text())

    org_el = soup.select_one("a.topcard__org-name-link")
    if org_el:
        detail.company_name = _clean(org_el.get_text())
        detail.company_url = _canonical_company_url(org_el.get("href"))
    else:
        org_el = soup.select_one(".topcard__flavor")
        if org_el:
            detail.company_name = _clean(org_el.get_text())

    location_el = soup.select_one(".topcard__flavor--bullet")
    if location_el:
        detail.location = _clean(location_el.get_text())

    applicants_el = soup.select_one(".num-applicants__caption") or soup.select_one(
        ".topcard__flavor--metadata.topcard__flavor--bullet"
    )
    if applicants_el:
        detail.applicants_raw = _clean(applicants_el.get_text())

    mapping = {
        "seniority level": "seniority",
        "employment type": "employment_type",
        "job function": "job_function",
        "industries": "industries",
        "уровень должности": "seniority",
        "тип занятости": "employment_type",
        "должностные обязанности": "job_function",
        "отрасли": "industries",
    }
    for item in soup.select("li.description__job-criteria-item"):
        key_el = item.select_one(".description__job-criteria-subheader")
        value_el = item.select_one(".description__job-criteria-text")
        if not key_el or not value_el:
            continue
        key = _clean(key_el.get_text()).lower()
        value = _clean(value_el.get_text())
        detail.criteria[key] = value
        attr = mapping.get(key)
        if attr:
            setattr(detail, attr, value)

    return detail


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str:
    selector = f'meta[name="{name}"]' if name else f'meta[property="{prop}"]'
    el = soup.select_one(selector)
    return _clean(el.get("content") if el else "")


def parse_company_profile(html: str) -> CompanyProfile:
    soup = BeautifulSoup(html, "lxml")
    profile = CompanyProfile()

    name_el = soup.select_one(".top-card-layout__title") or soup.select_one("h1")
    profile.name = _clean(name_el.get_text() if name_el else "")

    tagline_el = soup.select_one(".top-card-layout__second-subline") or soup.select_one(
        ".top-card-layout__headline"
    )
    profile.tagline = _clean(tagline_el.get_text() if tagline_el else "")

    desc_el = (
        soup.select_one('[data-test-id="about-us__description"]')
        or soup.select_one(".about-us__description")
        or soup.select_one(".core-section-container__content p")
    )
    if desc_el:
        profile.description = desc_el.get_text("\n", strip=True)

    for key, attr in (
        ("about-us__industry", "industry"),
        ("about-us__size", "company_size"),
        ("about-us__headquarters", "headquarters"),
        ("about-us__website", "website"),
    ):
        el = soup.select_one(f'[data-test-id="{key}"]')
        if not el:
            continue
        value_el = el.select_one("dd") or el
        value = _clean(value_el.get_text())
        if attr == "website":
            link = value_el.select_one("a") if hasattr(value_el, "select_one") else None
            value = _clean(link.get("href")) if link and link.get("href") else value
            value = value[:500]
        setattr(profile, attr, value[:255] if attr != "website" else value)

    followers_el = soup.select_one(".top-card-layout__first-subline .font-sans") or soup.select_one(
        '[data-test-id="about-us__followers"]'
    )
    if followers_el:
        profile.followers = _clean(followers_el.get_text())[:100]

    logo_el = soup.select_one(".top-card-layout__entity-image") or soup.select_one('img[class*="org-top-card"]')
    if logo_el:
        profile.logo_url = _clean(logo_el.get("data-delayed-url") or logo_el.get("src"))[:500]

    # Фолбэк на og:/meta-описание: "Name | 102,383 followers on LinkedIn. Tagline | Description"
    meta_description = _meta(soup, name="description") or _meta(soup, prop="og:description")
    if meta_description:
        if not profile.followers:
            m = re.search(r"([\d  ,.]+)\s+followers", meta_description)
            if m:
                profile.followers = _clean(m.group(1))[:100]
        parts = [p.strip() for p in meta_description.split("|") if p.strip()]
        if not profile.description and len(parts) >= 3:
            profile.description = parts[-1]
        if not profile.tagline and len(parts) >= 2:
            tail = parts[1]
            profile.tagline = _clean(re.sub(r"^[\d  ,.]+\s*followers on LinkedIn\.?", "", tail))[:500]
    if not profile.description and profile.tagline:
        profile.description = profile.tagline

    return profile


