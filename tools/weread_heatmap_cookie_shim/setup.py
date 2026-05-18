from setuptools import setup


setup(
    name="weread-heatmap-cookie-shim",
    version="0.1.1",
    py_modules=["weread_heatmap_cookie_shim"],
    install_requires=[
        "github-heatmap==1.0.8",
        "notion-client==2.4.0",
        "weread2notionpro==0.2.9",
    ],
    entry_points={
        "console_scripts": [
            "book=weread_heatmap_cookie_shim:book_main",
            "weread=weread_heatmap_cookie_shim:weread_main",
            "read_time=weread_heatmap_cookie_shim:read_time_main",
            "github_heatmap=weread_heatmap_cookie_shim:main",
        ],
    },
)
