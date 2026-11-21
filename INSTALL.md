# Install on a manager’s Windows laptop

Open **ChatGPT for Windows → Codex → Local** and choose a writable folder where the app should live. For example, create a folder called `Restaurant tools` in Documents. Paste this message into that local session. The download and setup work under the manager’s Windows account.

> Установи iiko Intelligence из официального релиза https://github.com/ktsAE/iiko-intelligence/releases/tag/v0.3.1 в выбранную локальную папку Windows. Сверь SHA-256 архива с GitHub, запусти Setup-iiko.cmd /quiet и проверь, что MCP отвечает. Сохрани существующие данные. Покажи мне, какую папку открыть в Codex и как проверить /mcp. Для первой проверки используй демоданные.

## For the local installation assistant

Use the pinned release’s [PowerShell installer](https://github.com/ktsAE/iiko-intelligence/blob/v0.3.1/Install-iiko.ps1). It downloads the Windows ZIP, verifies GitHub’s SHA-256 digest, and runs the bundled connector check. A second run of the same installer checks the existing installation and preserves its data. The resulting project folder is `iiko-intelligence` within the chosen parent. The only network requests during this installation are for GitHub release metadata and the release download.

If local script execution is restricted, download the [Windows ZIP](https://github.com/ktsAE/iiko-intelligence/releases/download/v0.3.1/iiko-intelligence-windows-x64.zip), verify its SHA-256 against the release, extract it into a new folder, then run `Setup-iiko.cmd /quiet` from the extracted `iiko-intelligence` folder. Existing installations have upgrade instructions in the [Windows guide](WINDOWS.md).

Open the **extracted project folder** in a new local Codex session, trust it and enter `/mcp`. Call `get_data_status` on `iiko_intelligence`. A new installation returns `synthetic`. The manager can then import a closed day from iikoOffice and reconcile the total. [OpenAI MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)

For ordinary browser chat, `Report-for-ChatGPT.cmd` creates a local text report the manager can review and attach. [ChatGPT file uploads](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)
