# Install on Windows from GitHub

Send the manager the prompt below through your usual trusted chat. Include the installer's SHA-256 in that message. She can paste it into local Codex on her Windows laptop. The installer fetches the signed package and creates the project in her Windows Documents folder.

> Установи iiko Intelligence из релиза ktsAE/iiko-intelligence v0.7.1 на GitHub. Скачай установщик из https://github.com/ktsAE/iiko-intelligence/releases/download/v0.7.1/Install-iiko.ps1 во временную папку. Проверь SHA-256 по значению из моего сообщения. При несовпадении остановись. После проверки запусти установщик с параметром `-ParentDirectory ([Environment]::GetFolderPath('MyDocuments'))`. Проверь MCP и покажи полный путь к проекту. Если там уже есть другая установка, сохрани её и её данные.

Publisher reference for this release's `Install-iiko.ps1`:

```text
79504ec1944a17471f24520779ab55b5565c730b341cbf578c296f362ca83332
```

Open **Documents → iiko-intelligence** in local Codex and trust this project. Windows may display Documents as **Документы** or redirect it to OneDrive. The installer prints its exact location. The first session uses demo data. Ask **«Проверь подключение к iiko и покажи, какие данные доступны»**.

For an offline installation, supply the ZIP and detached signature next to the verified installer and pass `-PackageDirectory .` from that folder. In both modes the installer checks the publisher signature before it extracts or runs the package.

## Moving from an unsigned version

Close old MCP sessions and import windows. Install into a **new parent folder**. Keep the old project as a backup. For an existing manager, copy the SQLite database using SQLite's backup operation into the new project's `data/manager.sqlite` after closing the new demo session. Under the same Windows account, copy the regular file `data/cloud-profile.dpapi` if present and any needed report text files. Keep the new project's code, runtime, Codex configuration and update state from the signed install. Run its `Setup-iiko.cmd /quiet` again and compare the restaurant, dates and reconciliation figures. Use the new folder for subsequent sessions.

## Future updates

Say **«Обнови iiko»** or use the usual reports. The connector checks for signed releases while it is open. Reopen the project when an update is ready. Unsigned local v0.4.1 requires the first signed installation described above.
