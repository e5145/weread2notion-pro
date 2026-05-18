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


def main():
    sys.argv = _inject_weread_cookie(sys.argv[:])
    return github_heatmap_main()
