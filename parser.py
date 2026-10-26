"""OpenSooq -> Telegram. Python 3.11+."""
import asyncio
import json
import logging
import math
import os
import re
import shlex
import sqlite3
import time
import unicodedata
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, MessageHandler, filters

COUNTRIES = "kw ae bh sa".split()
HOSTS = {"opensooq.com", "www.opensooq.com"} | {
    c + ".opensooq.com" for c in COUNTRIES
}
METRICS = {
    "price", "views", "seller_views", "rating",
    "ratings", "reviews", "sold", "photos"
}

MAX_PAGES = 2
MAX_ADS = 60
MAX_SEND = 20

CDN = "https://opensooq-imagesv2.os-cdn.com/previews/700x0/"
log = logging.getLogger("opensooq")


class SourceError(Exception):
    pass


class RateLimit(SourceError):
    pass


def safe_url(value, base=""):
    value = urljoin(base, str(value).strip())
    try:
        u = urlsplit(value)
        if (
            len(value) > 8192
            or "\\" in value
            or any(ord(c) < 32 for c in value)
            or u.scheme not in {"http", "https"}
            or u.hostname not in HOSTS
            or u.username
            or u.password
            or u.port not in {None, 80, 443}
        ):
            raise ValueError()
    except ValueError:
        raise ValueError(
            "Нужна веб-ссылка на категорию OpenSooq."
        ) from None

    return urlunsplit((
        "https", u.hostname, u.path or "/", u.query, ""
    ))


def photo_url(value, base=CDN):
    if not isinstance(value, str) or not value:
        return ""

    if not value.startswith(("https://", "http://", "//")):
        value = urljoin(base.replace("{size}", "700x0"), value)
        if value.lower().endswith((".jpg", ".jpeg", ".png")):
            value += ".webp"

    u = urlsplit(value)
    if (
        u.scheme not in {"http", "https"}
        or not u.hostname
        or not (
            u.hostname.endswith(".os-cdn.com")
            or u.hostname in HOSTS
        )
        or u.username
        or u.password
        or u.port not in {None, 80, 443}
    ):
        return ""

    return urlunsplit((
        "https", u.hostname, u.path, u.query, ""
    ))


def number(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        if value >= 0 and math.isfinite(value):
            return float(value)
        return None

    text = "".join(
        str(unicodedata.digit(c)) if c.isdecimal() else c
        for c in str(value)
    )
    text = text.replace("٬", "").replace("٫", ".").replace(",", "")
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)

    match = re.search(
        r"(\d+(?:\.\d+)?)(?:\s*(k|m|b|ألف|الف|مليون)(?!\w))?",
        text,
        re.I,
    )
    if not match or text.lstrip().startswith("-"):
        return None

    scales = {
        "k": 1000,
        "m": 1000000,
        "b": 1000000000,
        "ألف": 1000,
        "الف": 1000,
        "مليون": 1000000,
    }
    result = float(match[1]) * scales.get(
        (match[2] or "").lower(), 1
    )
    return result if math.isfinite(result) else None


def count(value):
    result = number(value)
    return round(result) if result is not None else None


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def props(soup):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return {}

    try:
        return json.loads(
            tag.string or tag.get_text()
        )["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        raise SourceError(
            "Изменились данные страницы OpenSooq."
        ) from None


def flight(soup):
    chunks = []
    result = []

    for tag in soup.find_all("script", src=False):
        text = tag.string or tag.get_text()

        for match in re.finditer(
            r"(?:self\.)?__next_f\.push\(", text
        ):
            try:
                data, _ = json.JSONDecoder().raw_decode(
                    text[match.end():]
                )
                if (
                    isinstance(data, list)
                    and len(data) > 1
                    and data[0] == 1
                    and isinstance(data[1], str)
                ):
                    chunks.append(data[1])
            except ValueError:
                pass

    for line in "".join(chunks).splitlines():
        key, separator, value = line.partition(":")
        if (
            separator
            and re.fullmatch(r"[0-9a-fA-F]+", key)
            and value[:1] in {"{", "["}
        ):
            with suppress(ValueError):
                result.append(json.loads(value))

    return result


def parse_category(raw, url):
    soup = BeautifulSoup(raw, "html.parser")
    data = props(soup)
    block = data.get("serpApiResponse", {}).get("listings")

    if (
        not isinstance(block, dict)
        or not isinstance(block.get("items"), list)
    ):
        raise SourceError(
            "Не найдена выдача. Нужна категория с объявлениями."
        )

    ads = []

    for row in block["items"]:
        if (
            not isinstance(row, dict)
            or not row.get("id")
            or not row.get("title")
        ):
            continue

        path = row.get("post_url") or f"/search/{row['id']}"

        if path.startswith("/search/"):
            locale = urlsplit(url).path.split("/")[1]
            locale = locale if locale in {"ar", "en"} else "en"
            path = "/" + locale + path

        image = photo_url(row.get("image_uri"))
        ad = {key: None for key in METRICS}

        ad.update(
            id=str(row["id"]),
            url=safe_url(path, url),
            title=str(row["title"]),
            description=str(row.get("masked_description") or ""),
            price=number(row.get("price_amount")),
            currency=str(row.get("price_currency_iso") or ""),
            photos=count(row.get("image_count")),
            images=[image] if image else [],
            views=count(row.get("views_count")),
            seller_id=str(row.get("member_id") or ""),
            seller_name=str(row.get("member_display_name") or ""),
            seller_url="",
            rating=number(row.get("member_rating_avg")),
            ratings=count(row.get("member_rating_count")),
        )

        ads.append(ad)

    if block["items"] and not ads:
        raise SourceError(
            "Изменилась структура карточек OpenSooq."
        )

    next_path = data.get("seoPage", {}).get("nextPageUrl")
    next_url = safe_url(next_path, url) if next_path else None
    return ads, next_url


def parse_listing(raw, ad):
    soup = BeautifulSoup(raw, "html.parser")
    data = None

    for obj in walk([props(soup)] + flight(soup)):
        candidate = obj.get("listingData")

        if (
            isinstance(candidate, dict)
            and str(
                candidate.get("commonInfo", {}).get("postId")
            ) == ad["id"]
        ):
            data = candidate
            break

    if data is None:
        raise SourceError(
            "Не удалось прочитать объявление."
        )

    info = data["commonInfo"]
    owner = info.get("ownerData") or {}

    ad.update(
        title=str(info.get("topTitle") or ad["title"]),
        description=str(
            info.get("description") or ad["description"]
        ),
    )

    for target, key, convert in (
        ("seller_id", "id", str),
        ("seller_name", "fullName", str),
        ("rating", "ratingAvg", number),
        ("ratings", "numberOfRatings", count),
    ):
        if owner.get(key) is not None:
            ad[target] = convert(owner[key])

    if owner.get("memberLink"):
        ad["seller_url"] = safe_url(
            owner["memberLink"], ad["url"]
        )

    gallery = info.get("gallery") or {}
    base = data.get("config", {}).get("imagesBaseUrl") or CDN

    images = [
        photo_url(media.get("uri"), base)
        for media in gallery.get("media", [])
        if (
            isinstance(media, dict)
            and str(media.get("mimeType", "")).startswith("image/")
        )
    ]

    ad["images"] = (
        list(dict.fromkeys(image for image in images if image))
        or ad["images"]
    )

    if gallery.get("chips", {}).get("imagesCount") is not None:
        ad["photos"] = count(gallery["chips"]["imagesCount"])

    for key in ("viewsCount", "views_count", "postViewsCount"):
        if info.get(key) is not None:
            ad["views"] = count(info[key])
            break

    listing = data.get("listing") or {}
    prices = listing.get("price", {}).get("prices", [])

    if prices:
        ad["price"] = number(prices[0].get("price"))
        ad["currency"] = str(
            prices[0].get("key")
            or prices[0].get("currencyLabel")
            or ad["currency"]
        )

    return ad


def parse_profile(raw, expected_id):
    soup = BeautifulSoup(raw, "html.parser")
    data = props(soup)

    member = (
        data.get("userInfo", {}).get("member")
        or data.get("data", {}).get("info", {}).get("member")
    )

    if (
        not isinstance(member, dict)
        or str(member.get("id")) != expected_id
    ):
        member = member if isinstance(member, dict) else {}

    rating = member.get("rating") or {}

    result = {
        "rating": number(rating.get("average_rating")),
        "ratings": count(rating.get("number_of_rating")),
        "reviews": count(rating.get("number_of_reviews")),
        "seller_views": count(member.get("views_count")),
        "sold": None,
    }

    for tag in soup(["script", "style"]):
        tag.decompose()
    page_text = soup.get_text(" ", strip=True)

    if result["seller_views"] is None:
        match_views = re.search(
            r"([\d.,٬٫]+\s*[kKmM]?)\s*(?:Views|مشاهدة|مشاهدات)",
            page_text,
            re.I,
        )
        if match_views:
            result["seller_views"] = count(match_views.group(1))

    if member.get("show_sold_listings") in (1, True, "1"):
        match_sold = re.search(
            r"(?:Sold\s+Listings|الإعلانات\s+المباعة|تم\s+بيعها)"
            r"\s*[(（]\s*([\d.,٬٫]+\s*[kKmM]?)\s*[)）]",
            page_text,
            re.I,
        )
        if match_sold:
            result["sold"] = count(match_sold.group(1))

    return result


def update_filters(current, text):
    result = dict(current)

    for pair in shlex.split(text):
        key, separator, value = pair.partition("=")
        metric, _, bound = key.rpartition("_")

        if not separator or (
            key not in {"keywords", "exclude", "currency", "unknown"}
            and not (
                metric in METRICS and bound in {"min", "max"}
            )
        ):
            raise ValueError(
                f"Неизвестный фильтр: {pair[:60]}"
            )

        if value.lower() in {"off", "none"}:
            result.pop(key, None)

        elif key in {"keywords", "exclude"}:
            if (
                len(value) > 500
                or not any(x.strip() for x in value.split(","))
            ):
                raise ValueError(
                    "Укажите слова через запятую."
                )
            result[key] = value

        elif key == "currency":
            if not re.fullmatch("[A-Za-z]{3}", value):
                raise ValueError(
                    "Валюта: код из 3 букв."
                )
            result[key] = value.upper()

        elif key == "unknown":
            if value not in {"include", "exclude"}:
                raise ValueError(
                    "unknown: только include или exclude."
                )
            result[key] = value

        else:
            number_value = float(value.replace(",", "."))

            if (
                not math.isfinite(number_value)
                or number_value < 0
                or (metric == "rating" and number_value > 5)
            ):
                raise ValueError(
                    "Числа должны быть неотрицательными."
                )

            if (
                metric not in {"price", "rating"}
                and not number_value.is_integer()
            ):
                raise ValueError(
                    "Счётчики должны быть целыми."
                )

            result[key] = number_value

    for metric in METRICS:
        if result.get(metric + "_min", 0) > result.get(
            metric + "_max", math.inf
        ):
            raise ValueError(
                f"{metric}_min больше {metric}_max."
            )

    return result


def matches(ad, rules):
    text = (
        ad["title"] + "\n" + ad.get("description", "")
    ).casefold()

    def terms(name):
        return [
            value.strip().casefold()
            for value in rules.get(name, "").split(",")
            if value.strip()
        ]

    if terms("keywords") and not any(
        value in text for value in terms("keywords")
    ):
        return False, set()

    if any(value in text for value in terms("exclude")):
        return False, set()

    missing = set()
    accepted = True

    if rules.get("currency"):
        if not ad.get("currency"):
            missing.add("currency")
        elif ad["currency"] != rules["currency"]:
            accepted = False

    for metric in METRICS:
        low = rules.get(metric + "_min")
        high = rules.get(metric + "_max")

        if low is None and high is None:
            continue

        value = ad.get(metric)

        if value is None:
            missing.add(metric)
        elif (
            (low is not None and value < low)
            or (high is not None and value > high)
        ):
            accepted = False

    allow_missing = not missing or rules.get("unknown") == "include"
    return accepted and allow_missing, missing


class Reader:
    def __init__(self):
        self.http = httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "OpenSooqTelegramBot/1.0 "
                    "(public listings monitor)"
                ),
                "Accept-Language": "en",
            },
        )
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.pauses = {}
        self.cache = {}

    async def get(self, url):
        url = safe_url(url)

        for _ in range(6):
            host = urlsplit(url).hostname

            if self.pauses.get(host, 0) > time.monotonic():
                raise RateLimit(
                    "OpenSooq ограничил запросы."
                )

            for attempt in range(3):
                async with self.lock:
                    await asyncio.sleep(
                        max(0, self.last + 2 - time.monotonic())
                    )
                    self.last = time.monotonic()

                try:
                    async with self.http.stream(
                        "GET", url
                    ) as response:
                        body = bytearray()

                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)

                            if len(body) > 8 * 1024 * 1024:
                                raise SourceError(
                                    "Слишком большой ответ."
                                )

                        raw = body.decode(
                            response.encoding or "utf-8",
                            errors="replace",
                        )

                    if response.status_code >= 500 and attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue

                    break

                except httpx.RequestError:
                    if attempt == 2:
                        raise SourceError(
                            "OpenSooq не отвечает."
                        ) from None

                    await asyncio.sleep(2 ** (attempt + 1))

            status = response.status_code

            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise SourceError(
                        "Перенаправление без адреса."
                    )

                url = safe_url(location, url)
                continue

            if status == 429:
                retry = response.headers.get("retry-after", "300")

                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (
                            parsedate_to_datetime(retry).timestamp()
                            - time.time()
                        )
                    except (ValueError, TypeError, OverflowError):
                        delay = 300

                self.pauses[host] = (
                    time.monotonic()
                    + max(60, min(3600, delay))
                )
                raise RateLimit(
                    "OpenSooq ограничил частоту запросов."
                )

            if status != 200:
                raise SourceError(
                    f"Страница недоступна: HTTP {status}."
                )

            if re.search(
                r"<title>\s*(?:Just a moment|Access Denied)",
                raw[:15000],
                re.I,
            ):
                raise SourceError(
                    "OpenSooq показал проверку доступа."
                )

            return raw, url

        raise SourceError(
            "Слишком много перенаправлений."
        )

    async def enrich(self, ad):
        raw, final = await self.get(ad["url"])

        if (
            urlsplit(final).hostname
            != urlsplit(ad["url"]).hostname
        ):
            raise SourceError(
                "Объявление перенаправило на другую страну."
            )

        ad["url"] = final
        ad = await asyncio.to_thread(parse_listing, raw, ad)

        warning = ""
        url = ad.get("seller_url")

        if url:
            cached = self.cache.get(url)

            if cached and cached[0] > time.monotonic():
                profile = cached[1]
            else:
                try:
                    raw, _ = await self.get(url)
                    profile = await asyncio.to_thread(
                        parse_profile, raw, ad["seller_id"]
                    )
                except RateLimit:
                    raise
                except (SourceError, ValueError):
                    profile = None

                self.cache[url] = (
                    time.monotonic() + 300, profile
                )

            if profile is not None:
                ad.update({
                    key: value
                    for key, value in profile.items()
                    if value is not None
                })
            else:
                warning = (
                    "Часть профилей недоступна."
                )

        if len(self.cache) > 2000:
            self.cache = {
                key: value
                for key, value in self.cache.items()
                if value[0] > time.monotonic()
            }
            self.cache = dict(
                list(self.cache.items())[-1000:]
            )

        return ad, warning


def card(ad):
    def show(key):
        value = ad.get(key)
        if value is None:
            return "нет данных"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    text = (
        f"{ad['title'][:120]}\n"
        f"💰 {show('price')} {ad.get('currency', '')}\n"
        f"📷 Фото: {show('photos')}\n"
        f"👁 Просмотры: {show('views')}\n\n"
        f"👤 Продавец: "
        f"{ad.get('seller_name', '')[:70] or 'нет данных'}\n"
        f"⭐ Рейтинг: {show('rating')}\n"
        f"Оценок: {show('ratings')}\n"
        f"Отзывов: {show('reviews')}\n"
        f"Продано: {show('sold')}\n"
        f"Просмотры профиля ≈ {show('seller_views')}"
    )

    return (
        text.encode("utf-16-le")[:1990]
        .decode("utf-16-le", errors="ignore")
    )


async def telegram_call(method, **kwargs):
    try:
        return await method(**kwargs)
    except RetryAfter as exc:
        if hasattr(exc.retry_after, "total_seconds"):
            delay = exc.retry_after.total_seconds()
        else:
            delay = exc.retry_after

        if delay > 60:
            raise

        await asyncio.sleep(float(delay) + 1)
        return await method(**kwargs)


HELP = """Отправьте ссылку на категорию OpenSooq с объявлениями.

/filters price_max=500 currency=AED rating_min=4 photos_min=2
/filters keywords="iphone,samsung" exclude="broken,parts"

Числовые фильтры:
price — цена
views — просмотры объявления
seller_views — просмотры профиля
rating — рейтинг продавца, 0–5
ratings — число оценок
reviews — число текстовых отзывов
sold — объявления с отметкой «продано»
photos — число фото

К параметру добавьте _min или _max.

/filters — текущие фильтры
/filters views_min=off — отключить границу
/filters unknown=include — пропускать неизвестные значения
/reset — сбросить фильтры
/sources — категории
/remove 1 — удалить категорию
/check — проверить сейчас
/watch — регулярные проверки
/interval 15 — интервал 5–1440 минут
/pause — остановить
/status — состояние
/id — ваш Telegram ID

Отправляется первое фото; все фото открываются по кнопке.
"""


class Service:
    def __init__(self, bot, allowed, path):
        self.bot = bot
        self.allowed = allowed

        Path(path).parent.mkdir(
            parents=True, exist_ok=True
        )
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

        self.db.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users(
                chat INTEGER PRIMARY KEY,
                urls TEXT NOT NULL DEFAULT '[]',
                rules TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 0,
                minutes INTEGER NOT NULL DEFAULT 15,
                next_check REAL NOT NULL DEFAULT 0,
                last TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS delivered(
                chat INTEGER NOT NULL,
                key TEXT NOT NULL,
                PRIMARY KEY(chat,key)
            );
        """)
        self.db.commit()

        self.reader = Reader()
        self.tasks = {}
        self.lock = asyncio.Lock()

    def state(self, chat):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO users(chat) VALUES (?)",
                (chat,),
            )

        row = dict(
            self.db.execute(
                "SELECT * FROM users WHERE chat=?",
                (chat,),
            ).fetchone()
        )
        row["urls"] = json.loads(row["urls"])
        row["rules"] = json.loads(row["rules"])
        return row

    def save(self, chat, **changes):
        allowed_fields = {
            "urls", "rules", "enabled",
            "minutes", "next_check", "last"
        }
        if not changes.keys() <= allowed_fields:
            raise ValueError(
                "Некорректные поля настроек."
            )

        values = [
            json.dumps(value, ensure_ascii=False)
            if key in {"urls", "rules"}
            else value
            for key, value in changes.items()
        ]

        with self.db:
            self.db.execute(
                "UPDATE users SET "
                + ",".join(key + "=?" for key in changes)
                + " WHERE chat=?",
                values + [chat],
            )

    def trigger(self, chat, notify=True):
        if chat not in self.allowed:
            return False

        if chat in self.tasks and not self.tasks[chat].done():
            return False

        state = self.state(chat)
        self.save(
            chat,
            next_check=time.time() + state["minutes"] * 60,
        )

        self.tasks[chat] = asyncio.create_task(
            self.scan(chat, notify)
        )
        return True

    async def tick(self, context):
        rows = list(
            self.db.execute(
                "SELECT chat FROM users "
                "WHERE enabled=1 AND next_check<=?",
                (time.time(),),
            )
        )
        for row in rows:
            self.trigger(row["chat"], notify=False)

    async def pause(self, chat):
        self.save(chat, enabled=0)
        task = self.tasks.get(chat)

        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def send(self, chat, ad):
        buttons = [[
            InlineKeyboardButton(
                "Объявление и все фото",
                url=ad["url"],
            )
        ]]

        if ad.get("seller_url"):
            buttons.append([
                InlineKeyboardButton(
                    "Продавец",
                    url=ad["seller_url"],
                )
            ])

        kwargs = {
            "chat_id": chat,
            "reply_markup": InlineKeyboardMarkup(buttons),
        }

        if ad.get("images"):
            try:
                await telegram_call(
                    self.bot.send_photo,
                    photo=ad["images"][0],
                    caption=card(ad),
                    **kwargs,
                )
                return
            except BadRequest:
                pass

        await telegram_call(
            self.bot.send_message,
            text=card(ad),
            **kwargs,
        )

    async def scan(self, chat, notify):
        checked = 0
        sent = 0
        errors = 0
        skipped = 0

        missing = set()
        warnings = set()
        seen = set()

        stop = False
        limited = False

        try:
            async with self.lock:
                for source in self.state(chat)["urls"]:
                    url = source
                    pages_seen = set()

                    for page_index in range(MAX_PAGES):
                        if (
                            url in pages_seen
                            or source not in self.state(chat)["urls"]
                        ):
                            break

                        pages_seen.add(url)

                        try:
                            raw, final = await self.reader.get(url)
                            ads, next_url = await asyncio.to_thread(
                                parse_category, raw, final
                            )
                        except (SourceError, ValueError) as exc:
                            errors += 1
                            warnings.add(str(exc))
                            stop = isinstance(exc, RateLimit)
                            break

                        for ad in ads:
                            key = (
                                urlsplit(ad["url"]).hostname
                                + ":" + ad["id"]
                            )

                            if key in seen:
                                continue

                            seen.add(key)

                            delivered = self.db.execute(
                                "SELECT 1 FROM delivered "
                                "WHERE chat=? AND key=?",
                                (chat, key),
                            ).fetchone()

                            if delivered:
                                skipped += 1
                                continue

                            if checked >= MAX_ADS or sent >= MAX_SEND:
                                stop = True
                                limited = True
                                break

                            checked += 1

                            try:
                                ad, warning = await self.reader.enrich(ad)
                                if warning:
                                    warnings.add(warning)
                            except (SourceError, ValueError) as exc:
                                errors += 1
                                warnings.add(str(exc))

                                if isinstance(exc, RateLimit):
                                    stop = True
                                    break

                                continue

                            if source not in self.state(chat)["urls"]:
                                break

                            accepted, absent = matches(
                                ad,
                                self.state(chat)["rules"],
                            )
                            missing.update(absent)

                            if accepted:
                                await self.send(chat, ad)

                                with self.db:
                                    self.db.execute(
                                        "INSERT OR IGNORE INTO delivered "
                                        "VALUES (?,?)",
                                        (chat, key),
                                    )

                                sent += 1
                                await asyncio.sleep(1.1)

                        if stop or not next_url:
                            break

                        if (
                            urlsplit(next_url).hostname
                            != urlsplit(final).hostname
                        ):
                            warnings.add(
                                "Неожиданный адрес следующей страницы."
                            )
                            break

                        url = next_url

                        if page_index + 1 == MAX_PAGES:
                            limited = True

                    if stop:
                        break

            summary = (
                f"Проверено: {checked}. "
                f"Отправлено: {sent}. "
                f"Уже отправлялись: {skipped}. "
                f"Ошибок: {errors}."
            )

            if missing:
                summary += (
                    "\nНе хватает данных для фильтров: "
                    + ", ".join(sorted(missing))
                )

            if limited:
                summary += (
                    "\nДостигнут лимит обхода."
                )

            if warnings:
                summary += (
                    "\n"
                    + "\n".join(sorted(warnings)[:3])
                )

            state = self.state(chat)
            self.save(
                chat,
                last=summary,
                next_check=time.time() + state["minutes"] * 60,
            )

            if notify or errors or missing or warnings:
                await telegram_call(
                    self.bot.send_message,
                    chat_id=chat,
                    text=summary[:3900],
                )

        except asyncio.CancelledError:
            raise

        except Forbidden:
            self.save(chat, enabled=0)

        except Exception as exc:
            log.error(
                "Проверка прервана: %s",
                type(exc).__name__,
            )
            with suppress(TelegramError):
                await self.bot.send_message(
                    chat_id=chat,
                    text=(
                        "Ошибка проверки. "
                        "Проверьте журнал."
                    ),
                )

        finally:
            self.tasks.pop(chat, None)

    async def close(self):
        tasks = list(self.tasks.values())

        for task in tasks:
            task.cancel()

        await asyncio.gather(
            *tasks, return_exceptions=True
        )
        await self.reader.http.aclose()
        self.db.close()


async def handle(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat or chat.type != "private":
        return

    parts = message.text.strip().split(maxsplit=1)
    if not parts:
        return

    command = parts[0].split("@")[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    service = context.application.bot_data["service"]

    if command == "/id" or user.id not in service.allowed:
        await message.reply_text(
            f"Ваш Telegram ID: {user.id}\n"
            "Добавьте его в ALLOWED_TELEGRAM_IDS."
        )
        return

    state = service.state(chat.id)

    try:
        if command in {"/start", "/help"}:
            await message.reply_text(HELP)

        elif command in {"/filters", "/set"}:
            if argument:
                state["rules"] = update_filters(
                    state["rules"], argument
                )
                service.save(
                    chat.id, rules=state["rules"]
                )

            text = "\n".join(
                f"{key}={value}"
                for key, value in state["rules"].items()
            )
            await message.reply_text(
                "Фильтры:\n"
                + (text or "не заданы")
                + "\n\nВсе параметры: /help"
            )

        elif command == "/reset":
            service.save(chat.id, rules={})
            await message.reply_text(
                "Фильтры сброшены."
            )

        elif command == "/sources":
            text = "\n".join(
                f"{index}. {url}"
                for index, url in enumerate(state["urls"], 1)
            )
            await message.reply_text(
                text[:3900]
                or "Отправьте ссылку на категорию.",
                disable_web_page_preview=True,
            )

        elif command == "/remove":
            index = int(argument) - 1

            if not 0 <= index < len(state["urls"]):
                raise ValueError(
                    "Номер отсутствует. См. /sources."
                )

            state["urls"].pop(index)
            service.save(
                chat.id, urls=state["urls"]
            )
            await message.reply_text(
                "Категория удалена."
            )

        elif command == "/interval":
            minutes = int(argument)

            if not 5 <= minutes <= 1440:
                raise ValueError(
                    "Интервал: от 5 до 1440."
                )

            service.save(
                chat.id,
                minutes=minutes,
                next_check=time.time() + minutes * 60,
            )
            await message.reply_text(
                f"Интервал: {minutes} мин."
            )

        elif command in {"/pause", "/stop"}:
            await service.pause(chat.id)
            await message.reply_text(
                "Мониторинг остановлен."
            )

        elif command == "/status":
            enabled = (
                "включён" if state["enabled"] else "выключен"
            )
            await message.reply_text(
                f"Мониторинг: {enabled}\n"
                f"Интервал: {state['minutes']} мин.\n"
                f"Источников: {len(state['urls'])}\n"
                + (state["last"] or "Проверок ещё не было.")
            )

        elif (
            command in {"/check", "/watch", "/add"}
            or message.text.strip().startswith(
                ("https://", "http://")
            )
        ):
            is_link = message.text.strip().startswith(
                ("https://", "http://")
            )

            if command == "/add" or is_link:
                url = safe_url(
                    argument
                    if command == "/add"
                    else message.text.strip()
                )

                if re.search(
                    r"/(?:search|lists)/\d+",
                    urlsplit(url).path,
                ):
                    raise ValueError(
                        "Нужна ссылка на категорию."
                    )

                if url not in state["urls"]:
                    if len(state["urls"]) >= 5:
                        raise ValueError(
                            "Максимум 5 категорий."
                        )

                    state["urls"].append(url)
                    service.save(
                        chat.id, urls=state["urls"]
                    )

            if not state["urls"]:
                raise ValueError(
                    "Сначала отправьте ссылку."
                )

            if command == "/watch":
                service.save(chat.id, enabled=1)

            started = service.trigger(chat.id)

            text = (
                "Мониторинг включён.\n"
                if command == "/watch"
                else ""
            )
            text += (
                "Проверка началась. Остановка: /pause"
                if started
                else "Проверка уже выполняется."
            )

            await message.reply_text(text)

        else:
            await message.reply_text(
                "Отправьте ссылку на категорию или /help."
            )

    except ValueError as exc:
        await message.reply_text(
            str(exc)[:1000] or "Проверьте параметры: /help"
        )


def build_app(token, allowed, database):
    async def setup(app):
        service = Service(app.bot, allowed, database)
        app.bot_data["service"] = service

        app.job_queue.run_repeating(
            service.tick, interval=15, first=5,
        )

        await app.bot.set_my_commands([
            BotCommand("start", "Начать"),
            BotCommand("filters", "Фильтры"),
            BotCommand("check", "Проверить сейчас"),
            BotCommand("watch", "Мониторинг"),
            BotCommand("pause", "Остановить"),
            BotCommand("sources", "Категории"),
            BotCommand("help", "Инструкция"),
            BotCommand("id", "Мой Telegram ID"),
        ])

    async def cleanup(app):
        service = app.bot_data.pop("service", None)
        if service:
            await service.close()

    async def on_error(update, context):
        log.error("Ошибка: %s", type(context.error).__name__)

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(False)
        .post_init(setup)
        .post_stop(cleanup)
        .post_shutdown(cleanup)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT, handle
        )
    )
    app.add_error_handler(on_error)

    return app


def main():
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("BOT_TOKEN", "").strip()

    if not token or ":" not in token:
        raise SystemExit("Добавьте BOT_TOKEN в Secrets.")

    try:
        allowed = {
            int(value.strip())
            for value in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(",")
            if value.strip()
        }
        if any(value <= 0 for value in allowed):
            raise ValueError()
    except ValueError:
        raise SystemExit("ALLOWED_TELEGRAM_IDS: только цифры.") from None

    try:
        app = build_app(
            token, allowed,
            os.getenv("DATABASE_PATH", "bot.sqlite3"),
        )
        app.run_polling(allowed_updates=["message"])

    except TelegramError as exc:
        raise SystemExit(f"Ошибка Telegram: {type(exc).__name__}.") from None


if __name__ == "__main__":
    main()