import os
import sys

from github_heatmap.cli import main as github_heatmap_main


def _inject_weread_cookie(argv):
    if len(argv) < 2 or argv[1] != "weread":
        return argv

    has_cookie_arg = any(
        arg == "--weread_cookie" or arg.startswith("--weread_cookie=")
        for arg in argv[2:]
    )
    cookie = os.getenv("WEREAD_COOKIE", "")
    if cookie and not has_cookie_arg:
        return argv[:2] + ["--weread_cookie", cookie] + argv[2:]

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
    argv = _inject_weread_cookie(sys.argv[:])
    sys.argv = argv
    result = github_heatmap_main()
    _ensure_weread_svg(argv)
    return result
