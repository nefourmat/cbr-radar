"""tests/conftest.py"""
import pytest

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: тесты требующие реальную сеть"
    )
