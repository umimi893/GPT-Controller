from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsScriptSafetyTests(unittest.TestCase):
    def test_runtime_watchdog_is_hidden_and_outside_user_session(self) -> None:
        text = (ROOT / "scripts" / "install-service.ps1").read_text(encoding="utf-8")
        self.assertIn("$watchdogPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM'", text)
        self.assertIn("-WindowStyle Hidden", text)
        self.assertIn("-Principal $watchdogPrincipal", text)

    def test_interactive_watchdog_is_hidden_and_outside_user_session(self) -> None:
        text = (ROOT / "scripts" / "install-interactive-host.ps1").read_text(encoding="utf-8")
        self.assertIn("$watchdogPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM'", text)
        self.assertIn("-WindowStyle Hidden", text)
        self.assertIn("-Principal $watchdogPrincipal", text)
        # The actual Interactive Host intentionally remains in the signed-in user session
        # and uses pythonw; only its watchdog must be moved out of that session.
        self.assertIn("New-ScheduledTaskPrincipal -UserId $current -LogonType Interactive", text)
        self.assertIn("venv\\Scripts\\pythonw.exe", text)

    def test_all_agent_watchdogs_use_inbox_windows_powershell_for_system(self) -> None:
        expected = "System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        for relative in ("scripts/install-service.ps1", "scripts/install-interactive-host.ps1"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("-WindowStyle Hidden", text, relative)
            self.assertIn("-UserId 'SYSTEM' -LogonType ServiceAccount", text, relative)
            self.assertIn(expected, text, relative)
            self.assertIn("-Execute $systemPowerShell", text, relative)
            self.assertNotIn("$pwsh = (Get-Command pwsh.exe", text, relative)

    def test_runtime_watchdog_detects_live_but_stuck_steps(self) -> None:
        text = (ROOT / "scripts" / "watchdog-task.ps1").read_text(encoding="utf-8")
        self.assertIn("StepTimeoutGraceSeconds", text)
        self.assertIn("step_started_at", text)
        self.assertIn("step_timeout_seconds", text)
        self.assertIn("live runtime stuck in step", text)
        self.assertIn("taskkill.exe /PID $runtimePid /T /F", text)
        self.assertIn("watchdog-recovery-latest.json", text)

    def test_watchdog_remains_windows_powershell_51_compatible(self) -> None:
        text = (ROOT / "scripts" / "watchdog-task.ps1").read_text(encoding="utf-8")
        self.assertIn("function ConvertTo-UtcOffset", text)
        self.assertNotIn("ConvertFrom-Json -DateKind", text)
        self.assertIn("| ConvertFrom-Json", text)

    def test_runtime_installer_retires_legacy_console_instance(self) -> None:
        text = (ROOT / "scripts" / "install-service.ps1").read_text(encoding="utf-8")
        self.assertIn("$existingTask = Get-ScheduledTask -TaskName $TaskName", text)
        self.assertIn("$legacyConsoleRuntime", text)
        self.assertIn("$desiredExecute", text)
        self.assertIn("Stop-ScheduledTask -TaskName $TaskName", text)
        self.assertIn("legacy console-backed runtime", text)
        self.assertIn("pythonw.exe", text)

    def test_interactive_installer_restarts_running_host_before_reinstall(self) -> None:
        text = (ROOT / "scripts" / "install-interactive-host.ps1").read_text(encoding="utf-8")
        self.assertIn("$existingTask = Get-ScheduledTask -TaskName $TaskName", text)
        self.assertIn("Stop-ScheduledTask -TaskName $TaskName", text)
        self.assertIn("agent_runtime\\.interactive_host", text)
        self.assertIn("taskkill.exe /PID $_.ProcessId /T /F", text)
        stop_pos = text.index("Stop-ScheduledTask -TaskName $TaskName")
        register_pos = text.index("Register-ScheduledTask -TaskName $TaskName", stop_pos)
        self.assertLess(stop_pos, register_pos)

    def test_one_click_updater_closes_on_success_and_pauses_only_on_failure(self) -> None:
        text = (ROOT / "Update GPT Controller.bat").read_text(encoding="utf-8")
        success = 'if "%RC%"=="0" (\n  echo Update finished successfully.\n  exit /b 0\n)'
        self.assertIn(success, text.replace("\r\n", "\n"))
        self.assertEqual(text.lower().count("pause"), 2)
        failure_marker = "echo Update failed with exit code %RC%."
        self.assertIn(failure_marker, text)
        self.assertLess(text.index(failure_marker), text.lower().rindex("pause"))

    def test_public_bootstrap_creates_separate_private_control_repository(self) -> None:
        install = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts" / "bootstrap-install.ps1").read_text(encoding="utf-8")
        self.assertNotIn("remote','get-url','origin", install)
        self.assertIn("'repo','create',$controlFullName,'--private'", bootstrap)
        self.assertIn("$visibility -ne 'PRIVATE'", bootstrap)
        self.assertIn("$Branch = 'gpt-controller-control'", bootstrap)
        self.assertIn("$env:COMPUTERNAME.ToLowerInvariant()", bootstrap)

    def test_update_repairs_missing_runtime(self) -> None:
        text = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")
        self.assertIn("Repair invariant: every successful update leaves the Runtime task installed", text)
        self.assertIn("& (Join-Path $PSScriptRoot 'install.ps1') -InstallRoot $InstallRoot", text)
        self.assertIn("& (Join-Path $PSScriptRoot 'install-service.ps1') -InstallRoot $InstallRoot -TaskName $RuntimeTask", text)
        self.assertNotIn("if ($hadRuntime) {\n            if ($runtimeWasSystem)", text)

    def test_update_kills_stale_runtime_tree_before_replacing_code(self) -> None:
        text = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")
        self.assertIn("function Stop-AgentRuntimeTree", text)
        self.assertIn("taskkill.exe /PID $runtimePid /T /F", text)
        self.assertIn("Get-CimInstance Win32_Process", text)
        self.assertIn("Stop-AgentRuntimeTree -TaskName $RuntimeTask", text)

    def test_update_requires_fresh_live_heartbeat_before_success(self) -> None:
        text = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")
        self.assertIn("function Wait-AgentRuntimeHealthy", text)
        self.assertIn("Runtime health handshake PASS", text)
        self.assertIn("Get-Process -Id $pidValue", text)
        self.assertIn("Wait-AgentRuntimeHealthy -TaskName $RuntimeTask", text)
        self.assertIn("'-m','pytest','-q'", text)
        self.assertNotIn("'-m','unittest','discover'", text)

    def test_update_smoke_test_avoids_windows_native_quote_loss(self) -> None:
        text = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")
        self.assertIn("print('Agent imports OK')", text)
        self.assertNotIn('print(\\"Agent imports OK\\")', text)
        self.assertIn("Windows PowerShell/native argv boundaries", text)


if __name__ == "__main__":
    unittest.main()
