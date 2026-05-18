import os
import shutil
import sys

from github_heatmap.cli import main as github_heatmap_main


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
    from requests.utils import cookiejar_from_dict
    from weread2notionpro import weread_api

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


def main():
    _cleanup_local_build_artifacts()
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
    from weread2notionpro.read_time import main as weread_read_time_main

    return weread_read_time_main()
