from setuptools import find_packages, setup

setup(
    name="profile-cli",
    version="1.0.0",
    python_requires=">=3.9",
    packages=find_packages(),
    install_requires=["click>=8"],
    entry_points={"console_scripts": ["profile-cli = profile_cli.main:main"]},
)
