"""Catalog part 2: per-world status list + in-app help (merged into i18n_catalog.CATALOG; same (en, de, ja) rule)."""

CATALOG_WORLDS = {
    "tab.worlds": ("Worlds", "Welten", "ワールド"),
    "w.title": ("Your GSVR worlds", "Deine GSVR-Welten", "GSVRワールド一覧"),
    "w.open": ("Open world", "Welt öffnen", "ワールドを開く"),
    "w.help": ("Help", "Hilfe", "ヘルプ"),
    "w.name.rose": ("ROSE Online", "ROSE Online", "ROSE Online"),
    "w.name.hl": ("Half-Life family (Half-Life, Opposing Force, Blue Shift, Sven Co-op ...)", "Half-Life-Familie (Half-Life, Opposing Force, Blue Shift, Sven Co-op ...)",
                  "Half-Life ファミリー(Half-Life、Opposing Force、Blue Shift、Sven Co-op など)"),
    "w.name.war3": ("Warcraft III VR", "Warcraft III VR", "Warcraft III VR(ウォークラフト3)"),
    "w.name.cs": ("Counter-Strike 1.6", "Counter-Strike 1.6", "Counter-Strike 1.6"),
    "w.name.ro": ("Ragnarok Online", "Ragnarok Online", "Ragnarok Online"),
    "ws.ready": ("ready", "bereit", "準備完了"),
    "ws.building": ("building {pct} %", "wird gebaut {pct} %", "作成中 {pct} %"),
    "ws.building_n": ("building ({done}/{total})", "wird gebaut ({done}/{total})", "作成中 ({done}/{total})"),
    "w.rebuild": ("Rebuild", "Neu bauen", "作り直す"),
    "ws.update": ("update needed", "Update nötig", "更新が必要"),
    "ws.notfound": ("game not found", "Spiel nicht gefunden", "ゲームが見つかりません"),
    "ws.error": ("error", "Fehler", "エラー"),
    "ws.stopped": ("helper not running", "Helfer läuft nicht", "ヘルパー停止中"),
    "wh.ready": ("Ready. Press \"Open world\". Maps are built on the fly when you enter them.",
                 "Bereit. Drücke \"Welt öffnen\". Karten werden beim Betreten automatisch gebaut.",
                 "準備完了です。「ワールドを開く」を押してください。マップは入ると自動で作られます。"),
    "wh.update": ("The game was updated. The packs are renewed automatically in the background - or press \"Rebuild\" to start it now.",
                  "Das Spiel wurde aktualisiert. Die Pakete werden automatisch im Hintergrund erneuert - oder drücke \"Neu bauen\", um es sofort zu starten.",
                  "ゲームが更新されました。パックはバックグラウンドで自動的に更新されます。今すぐ始めるには「作り直す」を押してください。"),
    "wh.stopped": ("Press Start in the top bar so the world can reach your PC.",
                   "Drücke oben auf Start, damit die Welt deinen PC erreichen kann.",
                   "ワールドがこのPCと通信できるよう、上部の「スタート」を押してください。"),
    "wh.building": ("Building - this only happens once. You can already open the world.",
                    "Wird gebaut - das passiert nur einmal. Du kannst die Welt schon öffnen.",
                    "作成中です(初回のみ)。ワールドはすでに開けます。"),
    "wh.error": ("Something went wrong: {why}", "Etwas ist schiefgelaufen: {why}", "問題が発生しました: {why}"),
    "help.title": ("GSVR help", "GSVR-Hilfe", "GSVRヘルプ"),
    "help.discord": ("Still stuck? Ask in our Discord help channel:\n{url}", "Hängst du noch? Frag im Hilfe-Kanal unseres Discord:\n{url}",
                     "解決しませんか? Discordのヘルプチャンネルで質問してください:\n{url}"),
    "help.open_discord": ("Open Discord", "Discord öffnen", "Discordを開く"),
    "help.copy_discord": ("Copy Discord link", "Discord-Link kopieren", "Discordのリンクをコピー"),
    "help.nf.goldsrc": ("1. Install the game on Steam (Half-Life, Counter-Strike 1.6 ...) and start it once.\n2. Press \"Search again\".\n"
                        "3. Still nothing? Press \"Choose folder\" and pick the folder that contains hl.exe (for example steamapps\\common\\Half-Life).",
                        "1. Installiere das Spiel auf Steam (Half-Life, Counter-Strike 1.6 ...) und starte es einmal.\n2. Drücke \"Erneut suchen\".\n"
                        "3. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Ordner mit hl.exe (z. B. steamapps\\common\\Half-Life).",
                        "1. Steamでゲーム(Half-Life、Counter-Strike 1.6 など)をインストールし、1回起動します。\n2. 「もう一度探す」を押します。\n"
                        "3. それでも見つからない場合は「フォルダを選ぶ」で hl.exe があるフォルダ(例: steamapps\\common\\Half-Life)を指定します。"),
    "help.nf.ro": ("1. Install your Ragnarok Online client and start it once so it finishes updating.\n2. Press \"Search again\".\n"
                   "3. Still nothing? Press \"Choose folder\" and pick the game folder - the one that contains the file data.grf (check in Explorer).",
                   "1. Installiere deinen Ragnarok-Online-Client und starte ihn einmal, bis er fertig aktualisiert hat.\n2. Drücke \"Erneut suchen\".\n"
                   "3. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Spielordner - den mit der Datei data.grf (im Explorer prüfen).",
                   "1. Ragnarok Online のクライアントをインストールし、更新が終わるまで1回起動します。\n2. 「もう一度探す」を押します。\n"
                   "3. それでも見つからない場合は「フォルダを選ぶ」で data.grf があるゲームフォルダを指定します(エクスプローラーで確認できます)。"),
    "help.nf.war3": ("1. Install Warcraft III (Reforged or the classic version) and start it once.\n2. Press \"Search again\".\n"
                     "3. Still nothing? Press \"Choose folder\" and pick the Warcraft III folder (the one with Warcraft III.exe or war3.mpq).",
                     "1. Installiere Warcraft III (Reforged oder die klassische Version) und starte es einmal.\n2. Drücke \"Erneut suchen\".\n"
                     "3. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Warcraft-III-Ordner (mit Warcraft III.exe oder war3.mpq).",
                     "1. Warcraft III(Reforgedまたはクラシック版)をインストールし、1回起動します。\n2. 「もう一度探す」を押します。\n"
                     "3. それでも見つからない場合は「フォルダを選ぶ」で Warcraft III のフォルダ(Warcraft III.exe または war3.mpq があるフォルダ)を指定します。"),
    "help.nf.rose": ("1. Press \"Search again\".\n2. Still nothing? Press \"Choose folder\" and pick the folder of your ROSE installation - the one that contains the file data.idx (check in Explorer).\n3. Help on Discord.",
                     "1. Drücke \"Erneut suchen\".\n2. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Ordner deiner ROSE-Installation - den mit der Datei data.idx (im Explorer prüfen).\n3. Hilfe gibt es auf Discord.",
                     "1. 「もう一度探す」を押します。\n2. それでも見つからない場合は「フォルダを選ぶ」でROSEをインストールしたフォルダ(data.idx があるフォルダ、エクスプローラーで確認できます)を指定します。\n3. ヘルプはDiscordで。"),
    "help.update": ("The packs are built from your game files. After a game update they are rebuilt automatically in the background "
                    "while the helper runs. \"Rebuild\" in the Worlds tab starts it right away.",
                    "Die Pakete werden aus deinen Spieldateien gebaut. Nach einem Spiel-Update werden sie automatisch im Hintergrund neu gebaut, "
                    "solange der Helfer läuft. \"Neu bauen\" im Tab Welten startet es sofort.",
                    "パックはお持ちのゲームのファイルから作られます。ゲーム更新後は、ヘルパーの動作中にバックグラウンドで自動的に作り直されます。"
                    "「ワールド」タブの「作り直す」で今すぐ開始できます。"),
    "help.stopped": ("The GSVR helper runs on your PC and gives the world its maps. Press the green Start button at the top and keep this window open while you play.",
                     "Der GSVR-Helfer läuft auf deinem PC und liefert der Welt ihre Karten. Drücke oben den grünen Start-Knopf und lass dieses Fenster beim Spielen offen.",
                     "GSVRヘルパーはこのPCで動き、ワールドにマップを渡します。上部の緑のスタートを押し、遊んでいる間はこのウィンドウを開いたままにしてください。"),
    "help.error": ("Open the Log tab and look at the last lines. Try Stop, then Start. If the same error comes back, copy the log (Copy button) and send it in the Discord help channel.",
                   "Öffne den Tab Log und sieh dir die letzten Zeilen an. Probiere Stopp, dann Start. Kommt derselbe Fehler wieder, kopiere das Log (Knopf Kopieren) und schick es in den Discord-Hilfekanal.",
                   "「ログ」タブの最後の数行を確認し、停止 → スタートを試してください。同じエラーが続く場合は、ログをコピー(コピーボタン)してDiscordのヘルプチャンネルに送ってください。"),
    "help.helper": ("Another program may be using the network ports (VPN or game launchers). Close it and press Start again. Windows can also reserve ports: restart the PC once.",
                    "Ein anderes Programm belegt vielleicht die Netzwerk-Ports (VPN oder Spiele-Launcher). Schließe es und drücke erneut Start. Windows kann Ports auch reservieren: starte den PC einmal neu.",
                    "他のプログラム(VPNやゲームランチャー)がネットワークポートを使っている可能性があります。閉じてからもう一度スタートを押してください。Windowsがポートを予約していることもあるので、PCを1回再起動してみてください。"),
}
