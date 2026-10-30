"""Shared fixtures for Family AI OS tests."""

from pathlib import Path
import yaml
import pytest


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def test_db_path(temp_dir):
    """Provide a path to a test SQLite database (file is not created)."""
    return temp_dir / "test_family.db"


@pytest.fixture
def sample_config_yaml(temp_dir):
    """Write a minimal config.yaml to a temporary directory and return its path."""
    config_data = {
        "app": {
            "name": "Family AI OS",
            "data_dir": "./data",
            "log_dir": "./logs",
            "prompt_dir": "./prompts",
        },
        "plugins": {
            "enabled": ["sop_reminder"],
            "auto_discover": True,
            "plugin_dir": "./plugins",
            "instances": {
                "sop_reminder": {
                    "enabled": False,
                    "schedule": None,
                    "notify_on": ["family"],
                },
            },
        },
        "webhooks": {
            "family": {
                "label": "🏠 家庭群",
                "description": "测试家庭群",
                "env_key": "WECHAT_FAMILY_URL",
            },
            "invest": {
                "label": "📈 投资群",
                "description": "测试投资群",
                "env_key": "WECHAT_INVEST_URL",
            },
        },
        "llm": {
            "default_model": "gpt-4o-mini",
            "reasoning_model": "gpt-4o",
            "monthly_budget_usd": 10.0,
            "cache_enabled": True,
            "cache_ttl_hours": 24,
            "budget_warn_threshold": 0.8,
            "budget_cutoff_threshold": 1.0,
        },
        "scheduler": {
            "backend": "apscheduler",
            "timezone": "Asia/Shanghai",
        },
        "notifier": {
            "channels": [
                {"type": "file", "enabled": True},
                {"type": "email", "enabled": False},
                {"type": "wechat", "enabled": False},
            ],
        },
    }
    config_path = temp_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f, default_flow_style=False)
    return config_path
