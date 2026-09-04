# GPT Controller public release checklist

1. Run the complete pytest suite on Windows.
2. Run Python compileall over `src/`.
3. Confirm the public repository has a fresh one-commit history for the initial release.
4. Confirm the canonical wire protocol remains `q-agent-v4`.
5. Confirm the default private control branch is `gpt-controller-control`.
6. Confirm default install paths use `C:/GPT-Controller` and `C:/ProgramData/GPT-Controller`.
7. Confirm Scheduled Task names use `GPT Controller`.
8. Confirm `Install GPT Controller.bat` creates/refuses control repositories based on PRIVATE visibility.
9. Confirm no personal project paths, private repository references, credentials, tokens, `.env` files or private control history are present.
10. Confirm `Update GPT Controller.bat` runs the tested updater/repair path.
11. Confirm GitHub Actions verification passes on the public `main` branch.
