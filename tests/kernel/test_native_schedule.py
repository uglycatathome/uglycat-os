"""原生定时任务计划生成（三平台）。"""

import pytest


def test_windows_plan_uses_schtasks(tmp_path):
    from family_os.kernel.native_schedule import build_plan

    plan = build_plan("windows", "08:00", repo_root=tmp_path)
    assert plan["platform"] == "windows"
    cmd = plan["commands"][0]
    assert cmd[0] == "schtasks"
    assert "08:00" in cmd
    assert "uglycat-os-news" in cmd
    assert str(tmp_path.resolve()) in cmd[-1]
    assert plan["uninstall_commands"][0][:3] == ["schtasks", "/Delete", "/F"]


def test_linux_plan_uses_cron_marker(tmp_path):
    from family_os.kernel.native_schedule import build_plan, MARKER

    plan = build_plan("linux", "07:30", repo_root=tmp_path)
    line = plan["cron_line"]
    assert line.startswith("30 7 * * *")
    assert MARKER in line
    assert "run news_digest --notify" in line


def test_macos_plan_writes_launchd_plist(tmp_path):
    from family_os.kernel.native_schedule import build_plan, LAUNCHD_LABEL

    plan = build_plan("macos", "08:00", repo_root=tmp_path)
    path, content = plan["files"][0]
    assert path.name == f"{LAUNCHD_LABEL}.plist"
    assert "<key>Hour</key><integer>8</integer>" in content
    assert "run.py" in content


def test_invalid_time_raises(tmp_path):
    from family_os.kernel.native_schedule import build_plan

    with pytest.raises(ValueError):
        build_plan("linux", "8点", repo_root=tmp_path)
    with pytest.raises(ValueError):
        build_plan("linux", "25:00", repo_root=tmp_path)


def test_plan_does_not_touch_system_on_dry_run(tmp_path, monkeypatch):
    """dry-run 只打印，不执行任何子命令。"""
    from family_os.kernel import native_schedule

    calls = []
    monkeypatch.setattr(native_schedule.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)
    plan = native_schedule.build_plan("windows", "08:00", repo_root=tmp_path)
    log = native_schedule.apply_plan(plan, dry_run=True)
    assert calls == []
    assert any("将执行" in l for l in log)
