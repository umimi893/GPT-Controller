param(
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

& $Python -m compileall -q src
if ($LASTEXITCODE -ne 0) { throw 'compileall failed.' }

& $Python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'pytest failed.' }

& $Python -c "import json, pathlib; [json.loads(p.read_text(encoding='utf-8')) for p in pathlib.Path('schemas').glob('*.json')]; print('schema JSON parse OK')"
if ($LASTEXITCODE -ne 0) { throw 'schema JSON parse failed.' }

Write-Host 'GPT Controller verification PASS' -ForegroundColor Green
