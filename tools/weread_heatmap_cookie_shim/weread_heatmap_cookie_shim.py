import argparse
import calendar
import datetime as dt
import html
import hashlib
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse

import requests
from github_heatmap.cli import main as github_heatmap_main


GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
GATEWAY_HOST = "i.weread.qq.com"
DEFAULT_GATEWAY_SKILL_VERSION = "1.0.3"
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_GATEWAY_IPV4_FORCED = False


class WeReadGatewayError(RuntimeError):
    pass


def _normalize_gateway_key(value):
    key = (value or "").strip().strip('"').strip("'")
    if key.lower().startswith("authorization:"):
        key = key.split(":", 1)[1].strip()
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1].strip()
    if key.lower().startswith("weread_api_key="):
        key = key.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def _gateway_key_from_env():
    return _normalize_gateway_key(os.getenv("WEREAD_API_KEY", ""))


def _force_gateway_ipv4():
    """GitHub-hosted runners can prefer IPv6 for WeRead, but often have no IPv6 route."""
    global _GATEWAY_IPV4_FORCED
    if _GATEWAY_IPV4_FORCED:
        return
    value = os.getenv("WEREAD_FORCE_IPV4", "1").strip().lower()
    if value in ("0", "false", "no", "off"):
        return

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == GATEWAY_HOST and family in (0, socket.AF_UNSPEC):
            return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    _GATEWAY_IPV4_FORCED = True


def _int_env(name, default):
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _arg_value(argv, name, default=None):
    prefix = name + "="
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return default


class GatewayWeReadApi:
    """Drop-in WeReadApi replacement backed by the official agent gateway."""

    def __init__(self):
        _force_gateway_ipv4()
        self.api_key = _gateway_key_from_env()
        if not self.api_key:
            raise Exception("Missing WEREAD_API_KEY. In PowerShell: $env:WEREAD_API_KEY='<your_key>'")
        self.skill_version = os.getenv("WEREAD_SKILL_VERSION", DEFAULT_GATEWAY_SKILL_VERSION).strip()
        if not self.skill_version:
            self.skill_version = DEFAULT_GATEWAY_SKILL_VERSION
        self.timeout = _int_env("WEREAD_API_TIMEOUT", 30)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "weread-heatmap-cookie-shim/gateway",
            }
        )
        self._readinfo_supported = True

    def _call(self, api_name, **params):
        payload = {"api_name": api_name, "skill_version": self.skill_version}
        payload.update(params)
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.post(GATEWAY_URL, json=payload, timeout=self.timeout)
                if not response.ok:
                    text = response.text[:400].replace("\r", " ").replace("\n", " ")
                    raise WeReadGatewayError("%s HTTP %s %s" % (api_name, response.status_code, text))
                data = response.json()
                self._raise_for_gateway_error(api_name, data)
                return data
            except Exception as exc:  # noqa: BLE001 - this shim keeps retries compact.
                last_error = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        raise WeReadGatewayError("%s failed after retries: %s" % (api_name, last_error))

    @staticmethod
    def _raise_for_gateway_error(api_name, data):
        if "upgrade_info" in data:
            message = data.get("upgrade_info", {}).get("message", "gateway skill requires upgrade")
            raise WeReadGatewayError("%s: %s" % (api_name, message))
        errcode = data.get("errcode")
        if errcode not in (None, 0):
            message = data.get("errmsg") or data.get("message") or data.get("errstr") or str(errcode)
            raise WeReadGatewayError("%s: %s" % (api_name, message))

    def get_bookshelf(self):
        data = self._call("/shelf/sync")
        if data.get("bookProgress") is None:
            progress = []
            for book in data.get("books") or []:
                progress.append(
                    {
                        "bookId": book.get("bookId"),
                        "readingTime": book.get("readingTime") or book.get("recordReadingTime"),
                    }
                )
            data["bookProgress"] = progress
        return data

    def get_notebooklist(self):
        books = []
        seen = set()
        last_sort = None
        while True:
            params = {"count": 100}
            if last_sort is not None:
                params["lastSort"] = last_sort
            data = self._call("/user/notebooks", **params)
            page = data.get("books") or []
            for book in page:
                book_id = book.get("bookId") or (book.get("book") or {}).get("bookId")
                marker = (book_id, book.get("sort"))
                if marker in seen:
                    continue
                seen.add(marker)
                books.append(book)
            if not data.get("hasMore") or not page:
                break
            next_sort = page[-1].get("sort")
            if not next_sort or next_sort == last_sort:
                break
            last_sort = next_sort
        books.sort(key=lambda item: item.get("sort") or 0)
        return books

    def get_bookinfo(self, bookId):
        data = self._call("/book/info", bookId=bookId)
        if isinstance(data.get("book"), dict) and not data.get("bookId"):
            return data["book"]
        return data

    def get_bookmark_list(self, bookId):
        data = self._call("/book/bookmarklist", bookId=bookId)
        return data.get("updated") or []

    def get_read_info(self, bookId):
        params = dict(
            noteCount=1,
            readingDetail=1,
            finishedBookIndex=1,
            readingBookCount=1,
            readingBookIndex=1,
            finishedBookCount=1,
            bookId=bookId,
            finishedDate=1,
        )
        if self._readinfo_supported:
            try:
                return self._call("/book/readinfo", **params)
            except WeReadGatewayError:
                self._readinfo_supported = False
        return self._get_progress_read_info(bookId)

    def _get_progress_read_info(self, bookId):
        data = self._call("/book/getprogress", bookId=bookId)
        book = data.get("book") if isinstance(data.get("book"), dict) else data
        progress = book.get("progress") or book.get("readingProgress") or 0
        result = {
            "bookId": bookId,
            "markedStatus": 4 if progress == 100 else 0,
            "readingProgress": progress,
            "readingTime": book.get("recordReadingTime") or book.get("readingTime") or 0,
            "lastReadingDate": book.get("updateTime"),
        }
        if book.get("finishTime"):
            result["finishedDate"] = book.get("finishTime")
        return result

    def get_review_list(self, bookId):
        reviews = []
        raw_reviews = self._get_review_pages(bookId)
        for item in raw_reviews:
            review = item.get("review") if isinstance(item, dict) else item
            if not isinstance(review, dict):
                continue
            review = dict(review)
            if not review.get("reviewId") and isinstance(item, dict):
                review["reviewId"] = item.get("reviewId")
            review.setdefault("bookId", bookId)
            if review.get("type") == 4 and "chapterUid" not in review:
                review["chapterUid"] = 1000000
            reviews.append(review)
        return reviews

    def get_review_list2(self, bookId):
        reviews = self.get_review_list(bookId)
        summary = [review for review in reviews if review.get("type") == 4]
        note_reviews = []
        for review in reviews:
            if review.get("type") != 1:
                continue
            item = dict(review)
            item["markText"] = item.get("content", "")
            note_reviews.append(item)
        return summary, note_reviews

    def _get_review_pages(self, bookId):
        reviews = []
        synckey = None
        seen_synckeys = set()
        while True:
            params = {"bookid": bookId, "count": 100}
            if synckey:
                params["synckey"] = synckey
            data = self._call("/review/list/mine", **params)
            reviews.extend(data.get("reviews") or [])
            next_synckey = data.get("synckey")
            if not data.get("hasMore") or not next_synckey or next_synckey in seen_synckeys:
                break
            seen_synckeys.add(next_synckey)
            synckey = next_synckey
        return reviews

    def get_api_data(self):
        read_times = {}
        now = dt.datetime.now()
        for year in self._read_data_years(now.year):
            last_month = now.month if year == now.year else 12
            for month in range(1, last_month + 1):
                base_time = int(dt.datetime(year, month, 15, 12, 0, 0).timestamp())
                data = self._call("/readdata/detail", mode="monthly", baseTime=base_time)
                for key, value in (data.get("readTimes") or {}).items():
                    read_times[str(key)] = value
        return {"readTimes": read_times}

    def get_year_read_times(self, year):
        read_times = {}
        for month in range(1, 13):
            base_time = int(dt.datetime(year, month, 15, 12, 0, 0).timestamp())
            data = self._call("/readdata/detail", mode="monthly", baseTime=base_time)
            for key, value in (data.get("readTimes") or {}).items():
                read_times[int(key)] = int(value or 0)
        return read_times

    def _read_data_years(self, current_year):
        raw = os.getenv("WEREAD_READDATA_YEARS") or os.getenv("WEREAD_READDATA_YEAR")
        if not raw:
            return [current_year]
        years = set()
        for part in re.split(r"[\s,;]+", raw):
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                if start.strip().isdigit() and end.strip().isdigit():
                    start_year = int(start)
                    end_year = int(end)
                    if start_year > end_year:
                        start_year, end_year = end_year, start_year
                    years.update(range(start_year, end_year + 1))
            elif part.isdigit():
                years.add(int(part))
        years = {year for year in years if 2000 <= year <= current_year}
        return sorted(years) or [current_year]

    def get_chapter_info(self, bookId):
        data = self._call("/book/chapterinfo", bookId=bookId)
        chapters = data.get("chapters") or data.get("updated") or []
        if not chapters and isinstance(data.get("data"), list):
            first = data["data"][0] if data["data"] else {}
            chapters = first.get("updated") or first.get("chapters") or []
        chapters = [dict(chapter) for chapter in chapters if isinstance(chapter, dict)]
        chapters.append(
            {
                "chapterUid": 1000000,
                "chapterIdx": 1000000,
                "updateTime": 0,
                "readAhead": 0,
                "title": "\u70b9\u8bc4",
                "level": 1,
            }
        )
        return {item["chapterUid"]: item for item in chapters if item.get("chapterUid") is not None}

    def transform_id(self, book_id):
        book_id = str(book_id)
        id_length = len(book_id)
        if re.match("^\\d*$", book_id):
            ary = []
            for i in range(0, id_length, 9):
                ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
            return "3", ary
        result = ""
        for i in range(id_length):
            result += format(ord(book_id[i]), "x")
        return "4", [result]

    def calculate_book_str_id(self, book_id):
        book_id = str(book_id)
        md5 = hashlib.md5()
        md5.update(book_id.encode("utf-8"))
        digest = md5.hexdigest()
        result = digest[0:3]
        code, transformed_ids = self.transform_id(book_id)
        result += code + "2" + digest[-2:]
        for index, transformed_id in enumerate(transformed_ids):
            hex_length_str = format(len(transformed_id), "x")
            if len(hex_length_str) == 1:
                hex_length_str = "0" + hex_length_str
            result += hex_length_str + transformed_id
            if index < len(transformed_ids) - 1:
                result += "g"
        if len(result) < 20:
            result += digest[0 : 20 - len(result)]
        md5 = hashlib.md5()
        md5.update(result.encode("utf-8"))
        result += md5.hexdigest()[0:3]
        return result

    def get_url(self, book_id):
        return "https://weread.qq.com/web/reader/%s" % self.calculate_book_str_id(book_id)


def _normalized_cookie(cookie):
    cookie = (cookie or "").strip().strip('"').strip("'")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    cookie = cookie.replace("\r", " ").replace("\n", " ")
    if cookie and "&" in cookie and ";" not in cookie:
        return cookie.replace("&", "; ")
    return cookie


def _normalize_weread_cookie_env():
    cookie = os.getenv("WEREAD_COOKIE", "")
    normalized = _normalized_cookie(cookie)
    if normalized != cookie:
        os.environ["WEREAD_COOKIE"] = normalized


def _install_weread_api_compat():
    from weread2notionpro import weread_api

    if _gateway_key_from_env():
        weread_api.WeReadApi = GatewayWeReadApi
        return

    from requests.utils import cookiejar_from_dict

    original_init = weread_api.WeReadApi.__init__

    def _parse_cookie_string(self):
        cookies = {}
        for part in _normalized_cookie(self.cookie).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if not key or key.lower() == "cookie":
                continue
            cookies[key] = value.strip()

        missing = [key for key in ("wr_vid", "wr_skey") if key not in cookies]
        if missing:
            print(
                "::warning::WEREAD_COOKIE is missing "
                + ", ".join(missing)
                + ". Copy the Cookie request header from an i.weread.qq.com request."
            )
        return cookiejar_from_dict(cookies)

    def _init(self):
        original_init(self)
        self.cookie = _normalized_cookie(self.cookie)
        self.session.cookies = self.parse_cookie_string()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://weread.qq.com",
                "Referer": "https://weread.qq.com/",
            }
        )

    weread_api.WeReadApi.parse_cookie_string = _parse_cookie_string
    weread_api.WeReadApi.__init__ = _init


def _disable_secret_setting_sync():
    from weread2notionpro import notion_helper

    def _safe_noop(self):
        return None

    notion_helper.NotionHelper.insert_to_setting_database = _safe_noop


def _is_heatmap_url(url):
    if not url:
        return False
    return url.startswith("https://heatmap.malinkang.com/") or (
        "raw.githubusercontent.com" in url and "/OUT_FOLDER/" in url
    )


def _extract_heatmap_image_url(url):
    parsed = urllib.parse.urlparse(url)
    image_urls = urllib.parse.parse_qs(parsed.query).get("image")
    if image_urls:
        return image_urls[0]
    return url


def _install_notion_heatmap_image_update():
    from weread2notionpro import notion_helper

    def _search_database(self, block_id):
        children = self.client.blocks.children.list(block_id=block_id)["results"]
        for child in children:
            child_type = child.get("type")
            if child_type == "child_database":
                self.database_id_dict[child.get("child_database").get("title")] = child.get("id")
            elif child_type == "embed":
                if _is_heatmap_url(child.get("embed", {}).get("url")):
                    self.heatmap_block_id = child.get("id")
            elif child_type == "image":
                image = child.get("image", {})
                external = image.get("external") or {}
                if _is_heatmap_url(external.get("url")):
                    self.heatmap_block_id = child.get("id")
            if child.get("has_children"):
                self.search_database(child["id"])

    def _update_heatmap(self, block_id, url):
        image_url = _extract_heatmap_image_url(url)
        block = self.client.blocks.retrieve(block_id=block_id)
        if block.get("type") == "image":
            return self.client.blocks.update(
                block_id=block_id,
                image={"type": "external", "external": {"url": image_url}},
            )

        if block.get("type") == "embed":
            parent = block.get("parent") or {}
            parent_id = parent.get("page_id") or parent.get("block_id")
            if parent_id:
                response = self.client.blocks.children.append(
                    block_id=parent_id,
                    after=block_id,
                    children=[
                        {
                            "object": "block",
                            "type": "image",
                            "image": {"type": "external", "external": {"url": image_url}},
                        }
                    ],
                )
                self.client.blocks.delete(block_id=block_id)
                results = response.get("results") or []
                if results:
                    self.heatmap_block_id = results[0].get("id")
                return response

        return self.client.blocks.update(block_id=block_id, embed={"url": url})

    notion_helper.NotionHelper.search_database = _search_database
    notion_helper.NotionHelper.update_heatmap = _update_heatmap


def _cleanup_local_build_artifacts():
    roots = {
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.getcwd(), "tools", "weread_heatmap_cookie_shim"),
    }
    for root in roots:
        for name in ("build", "weread_heatmap_cookie_shim.egg-info"):
            path = os.path.join(root, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)


def _inject_weread_cookie(argv):
    if len(argv) < 2 or argv[1] != "weread":
        return argv

    has_cookie_arg = any(
        arg == "--weread_cookie" or arg.startswith("--weread_cookie=")
        for arg in argv[2:]
    )
    cookie = os.getenv("WEREAD_COOKIE", "")
    if cookie and not has_cookie_arg:
        return argv[:2] + ["--weread_cookie", _normalized_cookie(cookie)] + argv[2:]

    return argv


def _ensure_weread_svg(argv):
    if len(argv) < 2 or argv[1] != "weread":
        return

    out_folder = os.path.join(os.getcwd(), "OUT_FOLDER")
    output_path = os.path.join(out_folder, "weread.svg")
    if os.path.exists(output_path):
        return

    os.makedirs(out_folder, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(
            '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="48mm" '
            'viewBox="0 0 200 48">'
            '<rect width="200" height="48" fill="#FFFFFF"/>'
            '<text x="10" y="26" font-family="Arial, sans-serif" font-size="8" '
            'fill="#000000">No WeRead heatmap data for this year</text>'
            "</svg>"
        )


def _date_from_bucket_timestamp(timestamp):
    return dt.datetime.fromtimestamp(int(timestamp)).date()


def _heatmap_level(seconds):
    if seconds <= 0:
        return 0
    if seconds < 10 * 60:
        return 1
    if seconds < 30 * 60:
        return 2
    if seconds < 60 * 60:
        return 3
    return 4


def _heatmap_color(seconds, colors):
    level = _heatmap_level(seconds)
    return colors[level]


def _format_duration(seconds):
    minutes = int(seconds) // 60
    hours = minutes // 60
    rest = minutes % 60
    if hours and rest:
        return "%sh %sm" % (hours, rest)
    if hours:
        return "%sh" % hours
    if rest:
        return "%sm" % rest
    return "0m"


def _generate_gateway_heatmap(argv):
    year = int(_arg_value(argv, "--year", os.getenv("YEAR") or dt.date.today().year))
    colors = [
        _arg_value(argv, "--dom-color", "#EBEDF0"),
        _arg_value(argv, "--track-color", "#ACE7AE"),
        _arg_value(argv, "--special-color1", "#69C16E"),
        _arg_value(argv, "--special-color2", "#549F57"),
        _arg_value(argv, "--special-color3", "#2F7D32"),
    ]
    background = _arg_value(argv, "--background-color", "#FFFFFF")
    text_color = _arg_value(argv, "--text-color", "#000000")
    name = _arg_value(argv, "--me", "WeRead")

    client = GatewayWeReadApi()
    read_times = client.get_year_read_times(year)
    by_date = {_date_from_bucket_timestamp(key): value for key, value in read_times.items()}
    total_seconds = sum(by_date.values())
    active_days = sum(1 for value in by_date.values() if value >= 60)

    first_day = dt.date(year, 1, 1)
    last_day = dt.date(year, 12, 31)
    grid_start = first_day - dt.timedelta(days=(first_day.weekday() + 1) % 7)
    grid_end = last_day + dt.timedelta(days=(5 - last_day.weekday()) % 7)
    week_count = ((grid_end - grid_start).days // 7) + 1

    cell = 10
    gap = 3
    left = 38
    top = 36
    width = left + week_count * (cell + gap) + 18
    height = top + 7 * (cell + gap) + 34

    month_labels = []
    seen_months = set()
    cursor = first_day
    while cursor <= last_day:
        if cursor.month not in seen_months:
            week_index = ((cursor - grid_start).days // 7)
            month_labels.append(
                '<text x="%s" y="26" fill="%s" font-size="10">%s</text>'
                % (
                    left + week_index * (cell + gap),
                    text_color,
                    calendar.month_abbr[cursor.month],
                )
            )
            seen_months.add(cursor.month)
        cursor += dt.timedelta(days=1)

    weekday_labels = []
    for day_index, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        weekday_labels.append(
            '<text x="8" y="%s" fill="%s" font-size="9">%s</text>'
            % (top + day_index * (cell + gap) + 9, text_color, label)
        )

    rects = []
    cursor = grid_start
    while cursor <= grid_end:
        week_index = ((cursor - grid_start).days // 7)
        day_index = (cursor.weekday() + 1) % 7
        seconds = by_date.get(cursor, 0) if cursor.year == year else 0
        color = _heatmap_color(seconds, colors) if cursor.year == year else background
        title = "%s: %s" % (cursor.isoformat(), _format_duration(seconds))
        rects.append(
            '<rect x="%s" y="%s" width="%s" height="%s" rx="2" ry="2" fill="%s">'
            '<title>%s</title></rect>'
            % (
                left + week_index * (cell + gap),
                top + day_index * (cell + gap),
                cell,
                cell,
                color,
                html.escape(title),
            )
        )
        cursor += dt.timedelta(days=1)

    summary = "%s %s WeRead: %s, %s active days" % (
        html.escape(str(name)),
        year,
        _format_duration(total_seconds),
        active_days,
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">'
        '<rect width="100%%" height="100%%" fill="%s"/>'
        '<text x="8" y="16" fill="%s" font-family="Arial, sans-serif" font-size="12">%s</text>'
        '<g font-family="Arial, sans-serif">%s%s%s</g>'
        "</svg>"
    ) % (
        width,
        height,
        width,
        height,
        background,
        text_color,
        summary,
        "".join(month_labels),
        "".join(weekday_labels),
        "".join(rects),
    )

    out_folder = os.path.join(os.getcwd(), "OUT_FOLDER")
    os.makedirs(out_folder, exist_ok=True)
    output_path = os.path.join(out_folder, "weread.svg")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print("Generated WeRead heatmap from gateway data: %s" % output_path)
    return 0


def main():
    _cleanup_local_build_artifacts()
    if len(sys.argv) > 1 and sys.argv[1] == "weread" and _gateway_key_from_env():
        return _generate_gateway_heatmap(sys.argv[:])
    argv = _inject_weread_cookie(sys.argv[:])
    sys.argv = argv
    result = github_heatmap_main()
    _ensure_weread_svg(argv)
    return result


def book_main():
    _normalize_weread_cookie_env()
    _install_weread_api_compat()
    _disable_secret_setting_sync()
    from weread2notionpro.book import main as weread_book_main

    return weread_book_main()


def weread_main():
    _normalize_weread_cookie_env()
    _install_weread_api_compat()
    _disable_secret_setting_sync()
    from weread2notionpro.weread import main as weread_note_main

    return weread_note_main()


def read_time_main():
    _normalize_weread_cookie_env()
    _install_weread_api_compat()
    _disable_secret_setting_sync()
    _install_notion_heatmap_image_update()
    from weread2notionpro.read_time import main as weread_read_time_main

    return weread_read_time_main()


def _sync_missing_env():
    missing = []
    if not _gateway_key_from_env() and not os.getenv("WEREAD_COOKIE"):
        missing.append("WEREAD_API_KEY")
    if not os.getenv("NOTION_TOKEN"):
        missing.append("NOTION_TOKEN")
    if not os.getenv("NOTION_PAGE"):
        missing.append("NOTION_PAGE")
    return missing


def sync_main():
    parser = argparse.ArgumentParser(description="Sync WeRead books and notes to Notion.")
    parser.add_argument("--skip-books", action="store_true", help="Do not sync bookshelf/book metadata.")
    parser.add_argument("--skip-notes", action="store_true", help="Do not sync highlights and reviews.")
    parser.add_argument("--read-time", action="store_true", help="Also sync daily read-time data.")
    args = parser.parse_args()

    missing = _sync_missing_env()
    if missing:
        print(
            "Missing environment variables: %s" % ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "PowerShell example: $env:WEREAD_API_KEY='<key>'; "
            "$env:NOTION_TOKEN='<secret>'; $env:NOTION_PAGE='<page_url_or_id>'",
            file=sys.stderr,
        )
        return 2

    _normalize_weread_cookie_env()
    _install_weread_api_compat()
    _disable_secret_setting_sync()

    if not args.skip_books:
        print("== Syncing WeRead bookshelf to Notion ==")
        from weread2notionpro.book import main as weread_book_main

        weread_book_main()

    if not args.skip_notes:
        print("== Syncing WeRead notes to Notion ==")
        from weread2notionpro.weread import main as weread_note_main

        weread_note_main()

    if args.read_time:
        print("== Syncing WeRead read-time data to Notion ==")
        from weread2notionpro.read_time import main as weread_read_time_main

        weread_read_time_main()

    return 0
