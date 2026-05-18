from setuptools import setup


setup(
    name="weread-heatmap-cookie-shim",
    version="0.1.0",
    py_modules=["weread_heatmap_cookie_shim"],
    install_requires=["github-heatmap==1.0.8"],
    entry_points={
        "console_scripts": [
            "github_heatmap=weread_heatmap_cookie_shim:main",
        ],
    },
)
