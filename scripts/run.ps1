param([string]$Config = 'C:\GPT-Controller\agent.config.json')
$ErrorActionPreference = 'Stop'
& 'C:\GPT-Controller\venv\Scripts\gpt-controller.exe' --config $Config
