from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_rejects_windows_store_python_alias_and_probes_real_python():
    text = (ROOT / "scripts" / "bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "Microsoft\\\\WindowsApps\\\\python" in text
    assert "Test-PythonExecutable" in text
    assert "sys.version_info.major" in text
    assert "Ensure-Python" in text


def test_bootstrap_configures_gh_as_git_credential_helper():
    text = (ROOT / "scripts" / "bootstrap-install.ps1").read_text(encoding="utf-8")
    assert "@('auth','setup-git')" in text
    assert "Authenticated GitHub user:" in text


def test_failed_one_click_install_surfaces_persistent_log():
    bat = (ROOT / "Install GPT Controller.bat").read_text(encoding="utf-8")
    ps1 = (ROOT / "scripts" / "bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "C:\\GPT-Controller\\logs\\install.log" in bat
    assert "Get-Content" in bat
    assert "install.log" in ps1
    assert "Start-Transcript" in ps1
    assert "GPT Controller installation FAILED." in ps1
