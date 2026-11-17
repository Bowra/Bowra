# OpenAPI-Zugriff des Teams verwalten

Team-Eigentümer und Administratoren öffnen **Team-Einstellungen → OpenAPI**, sofern verfügbar.

1. Legen Sie in der Schlüsselverwaltung einen benannten Schlüssel mit den benötigten Berechtigungen und einer Laufzeit an. Ohne Ablaufdatum gilt der Standard von 30 Tagen. Für eine Pipeline, die Agent-Änderungen vorschlägt und evaluiert, gibt es die Vorlage Agent CI.
2. Kopieren Sie das vollständige Token, wenn es angezeigt wird, und speichern Sie es bei den Zugangsdaten des aufrufenden Systems. Es wird nur einmal angezeigt.
3. Die API-Dokumentation nennt Basispfad, Endpunkt, Berechtigung und Beispiele. Verwenden Sie das Token als Bearer-Token und die aus dem aufrufenden System erreichbare Dienstadresse samt Pfadpräfix. Die Referenz ist als Markdown verfügbar.
4. Prüfen Sie bei einem Fehler Schlüssel, Aktion und Ergebnis im Aufrufprotokoll sowie Berechtigungen und Ablaufdatum.

Deaktivieren oder widerrufen Sie einen Schlüssel, um dessen Zugriff zu stoppen, auch für eine Pipeline. Beim Erneuern wird das alte Token ersetzt. Aktualisieren Sie das aufrufende System. Historische Aufrufprotokolle bleiben beim Widerruf erhalten.
