#!/usr/bin/env python3
from setuptools import setup, find_packages

setup(
    name='iopipe_install',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        'click',
    ],
    entry_points='''
        [console_scripts]
        iopipe-install=iopipe_install.cli:main
    ''',
)
