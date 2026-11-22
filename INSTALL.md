# Installing the first signed version

Version 0.4.2 establishes the publisher key on the manager's Windows laptop. For the first installation, obtain the bootstrap script or its SHA-256 from Aleksandr through a separate trusted channel. A digest supplied alongside an untrusted download cannot establish who published it.

In local Codex on Windows, open the parent folder and give it the expected bootstrap SHA-256 from Aleksandr's message:

> Установи подписанную версию iiko Intelligence в новую папку. Проверь SHA-256 файла Install-iiko.ps1 по значению из моего сообщения, затем запусти этот установщик. Пакет и подпись находятся рядом с ним. Проверь подключение MCP и покажи, какую папку открыть.

For a local kit, compare the bootstrap hash with `Get-FileHash -Algorithm SHA256 -LiteralPath .\Install-iiko.ps1`. After it matches, run the verified script from the kit folder:

```powershell
.\Install-iiko.ps1 -PackageDirectory . -ParentDirectory "$env:USERPROFILE\Documents"
```

The parent must exist. The script reads `iiko-intelligence-windows-x64.zip` and its matching `.zip.sig`. Once the signed release is published, omitting `-PackageDirectory` fetches these assets from the pinned GitHub release. Add `-VerifyOnly` for verification without installation. The bootstrap validates the independent publisher signature and the entire ZIP before extracting or executing the package. Open the resulting `iiko-intelligence` project in local Codex. Use `get_data_status` to verify the running version.

## Moving from an unsigned version

Close old MCP sessions and import windows. Install into a **new parent folder**. Keep the old project as a backup. For an existing manager, copy the SQLite database using SQLite's backup operation into the new project's `data/manager.sqlite` after closing the new demo session. Under the same Windows account, copy the regular file `data/cloud-profile.dpapi` if present and any needed report text files. Keep the new project's code, runtime, Codex configuration and update state from the signed install. Run its `Setup-iiko.cmd /quiet` again and compare the restaurant, dates and reconciliation figures. Use the new folder for subsequent sessions.

## Future updates

Say **«Обнови iiko»** or use the usual reports. The connector checks for signed releases while it is open. Reopen the project when an update is ready. Unsigned local v0.4.1 requires the first signed installation described above.
