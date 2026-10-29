"""doctor 体检命令：AI 安装代理的唯一判据。"""

import json

import yaml


def _write_config(tmp_path, data_dir=None):
    config_data = {
        "app": {"name": "UglyCat OS", "data_dir": str(data_dir or tmp_path / "data"),
                "log_dir": str(tmp_path / "logs"), "prompt_dir": str(tmp_path / "prompts")},
        "plugins": {"enabled": ["news_digest"], "auto_discover": True,
                    "plugin_dir": "./plugins"},
        "webhooks": {},
        "llm": {"default_model": "glm-4.7-flash", "reasoning_model": "glm-4.7-flash"},
        "scheduler": {"backend": "apscheduler", "timezone": "Asia/Shanghai"},
        "notifier": {"channels": [{"type": "file", "enabled": True}]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data, allow_unicode=True), encoding="utf-8")
    return path


def test_doctor_passes_with_valid_config(tmp_path, monkeypatch):
    from family_os.kernel.doctor import run_doctor

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    config_path = _write_config(tmp_path)
    result = run_doctor(config_path)
    assert result["ok"] is True
    names = {c["name"]: c for c in result["checks"]}
    assert names["python"]["ok"] is True
    assert names["config"]["ok"] is True
    assert names["data_dir"]["ok"] is True
    assert names["llm_key"]["ok"] is True
    assert json.loads(json.dumps(result))["ok"] is True


def test_doctor_fails_without_llm_key(tmp_path, monkeypatch):
    from family_os.kernel.doctor import run_doctor

    for key in ("ZHIPU_API_KEY", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    # .env 按 cwd 解析；切到空目录以免读到开发机本地 .env
    monkeypatch.chdir(tmp_path)
    result = run_doctor(_write_config(tmp_path))
    assert result["ok"] is False
    llm = next(c for c in result["checks"] if c["name"] == "llm_key")
    assert llm["ok"] is False


def test_doctor_fails_when_config_missing(tmp_path):
    from family_os.kernel.doctor import run_doctor

    result = run_doctor(tmp_path / "nope.yaml")
    assert result["ok"] is False
    assert any(c["name"] == "config" and not c["ok"] for c in result["checks"])


def test_doctor_sources_skipped_by_default(tmp_path, monkeypatch):
    from family_os.kernel.doctor import run_doctor

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    result = run_doctor(_write_config(tmp_path))
    src = next(c for c in result["checks"] if c["name"] == "sources")
    assert src["ok"] is True and "跳过" in src["detail"]
