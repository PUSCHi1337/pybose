import os
from setuptools import setup, find_packages

version = os.getenv("PACKAGE_VERSION", "0.0.0")

setup(
    name="pybose",
    version=version,
    description="An unofficial Python API for controlling Bose soundbars and speakers.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="cavefire",
    author_email="timo@cavefire.net",
    maintainer="PUSCHi1337",
    url="https://github.com/PUSCHi1337/pybose",
    project_urls={
        "Upstream": "https://github.com/cavefire/pybose",
        "Fork changes": "https://github.com/PUSCHi1337/pybose/pulls",
    },
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "zeroconf",
        "websockets",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)