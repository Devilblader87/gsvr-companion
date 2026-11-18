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
    "ws.ready": ("ready", "bereit", "準備完了"),
    "ws.building": ("building {pct} %", "wird gebaut {pct} %", "作成中 {pct} %"),
    "ws.update": ("update needed", "Update nötig", "更新が必要"),
    "ws.notfound": ("game not found", "Spiel nicht gefunden", "ゲームが見つかりません"),
    "ws.error": ("error", "Fehler", "エラー"),
    "ws.stopped": ("helper not running", "Helfer läuft nicht", "ヘルパー停止中"),
    "wh.ready": ("Ready. Press \"Open world\". Maps are built on the fly when you enter them.",
                 "Bereit. Drücke \"Welt öffnen\". Karten werden beim Betreten automatisch gebaut.",
                 "準備完了です。「ワールドを開く」を押してください。マップは入ると自動で作られます。"),
    "wh.update": ("The game was updated. Press Start (or \"Build all\") once to refresh the packs.",
                  "Das Spiel wurde aktualisiert. Drücke einmal Start (oder \"Alle bauen\"), um die Pakete zu erneuern.",
                  "ゲームが更新されました。スタート(または「すべて作成」)を1回押して、パックを更新してください。"),
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
    "help.nf.war3": ("1. Install Warcraft III (Reforged or the classic version) and start it once.\n2. Press \"Search again\".\n"
                     "3. Still nothing? Press \"Choose folder\" and pick the Warcraft III folder (the one with Warcraft III.exe or war3.mpq).",
                     "1. Installiere Warcraft III (Reforged oder die klassische Version) und starte es einmal.\n2. Drücke \"Erneut suchen\".\n"
                     "3. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Warcraft-III-Ordner (mit Warcraft III.exe oder war3.mpq).",
                     "1. Warcraft III(Reforgedまたはクラシック版)をインストールし、1回起動します。\n2. 「もう一度探す」を押します。\n"
                     "3. それでも見つからない場合は「フォルダを選ぶ」で Warcraft III のフォルダ(Warcraft III.exe または war3.mpq があるフォルダ)を指定します。"),
    "help.nf.rose": ("1. Install a ROSE Online client (SearchMMO, jROSE or iROSE) and start it once so it finishes updating.\n2. Press \"Search again\".\n"
                     "3. Still nothing? Press \"Choose folder\" and pick the game folder - the one that contains the file data.idx (check in Explorer).",
                     "1. Installiere einen ROSE-Online-Client (SearchMMO, jROSE oder iROSE) und starte ihn einmal, bis er fertig aktualisiert hat.\n2. Drücke \"Erneut suchen\".\n"
                     "3. Immer noch nichts? Drücke \"Ordner wählen\" und wähle den Spielordner - den mit der Datei data.idx (im Explorer prüfen).",
                     "1. ROSE Online のクライアント(SearchMMO、jROSE、iROSE)をインストールし、更新が終わるまで1回起動します。\n2. 「もう一度探す」を押します。\n"
                     "3. それでも見つからない場合は「フォルダを選ぶ」で data.idx があるゲームフォルダを指定します(エクスプローラーで確認できます)。"),
    "help.update": ("The packs are built from your game files. After a game update they must be rebuilt: press Start on the first-run screen, "
                    "or \"Build all ROSE zones now\" in the Games tab. It runs in the background.",
                    "Die Pakete werden aus deinen Spieldateien gebaut. Nach einem Spiel-Update müssen sie neu gebaut werden: Start auf dem "
                    "Einrichtungsbildschirm oder \"Alle ROSE-Zonen jetzt bauen\" im Tab Spiele. Läuft im Hintergrund.",
                    "パックはお持ちのゲームのファイルから作られます。ゲームが更新されたら作り直しが必要です。かんたんセットアップのスタート、"
                    "または「ゲーム」タブの「ROSEの全ゾーンを今すぐ作成」を押してください(バックグラウンドで実行されます)。"),
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
