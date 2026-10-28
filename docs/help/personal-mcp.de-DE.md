# Persönlichen MCP-Dienst verbinden

Verbinden Sie einen entfernten MCP-Dienst, damit Ihr privater Agent externe Tools bei der Arbeit verwenden kann. Die Verbindung ist nur für Sie sichtbar. Teamverantwortliche und Teamadministratoren können weder die Verbindung einsehen oder konfigurieren noch Aufrufe genehmigen.

## Verbindung hinzufügen und Tools auswählen

1. Öffnen Sie **Capability Center → Meine Verbindungen** (`/connections/`).
2. Geben Sie einen Namen und die Adresse des entfernten MCP-Dienstes ein. Wählen Sie keine Authentifizierung, Bearer-Token oder API-Schlüssel. Bei einem API-Schlüssel können Sie den vom Dienst geforderten Headernamen verwenden.
3. Wählen Sie **Verbinden und Tools suchen**, markieren Sie die benötigten Tools und speichern Sie.

Gespeicherte Zugangsdaten können nicht erneut angezeigt werden. Lassen Sie die Felder beim Bearbeiten leer, damit die Zugangsdaten erhalten bleiben. Geben Sie neue Zugangsdaten ein, wenn Sie die Authentifizierungsmethode ändern. Bowra unterstützt entfernte MCP-Dienste über HTTP(S), keine lokalen Befehle. Cloud-Bereitstellungen erfordern öffentliches HTTPS; bei internen Bereitstellungen sind interne Adressen zulässig. Verwenden Sie nur vertrauenswürdige Dienste und Zugangsdaten mit den geringstmöglichen Rechten.

## Eigenes Slack-Konto verbinden

Wenn die Teamadministration Slack aktiviert und die Bereitstellung diese Funktion freigeschaltet hat, erscheint **Mein Slack verbinden** unter **Meine Verbindungen**. Schließen Sie die Autorisierung mit Ihrem eigenen Slack-Konto ab. Verbindung und Token gehören nur Ihnen; Teamverantwortliche und Teamadministratoren können sie weder einsehen noch verwenden.

Nach der Rückkehr zu **Meine Verbindungen** erklärt die Seite, ob die Verbindung erfolgreich war und was Sie bei einem Fehler prüfen oder erneut versuchen können. Falls es weiterhin nicht funktioniert, bitten Sie eine teamadministrierende Person, den letzten Schritt **Persönliches Slack-MCP aktivieren** unter **Team-Einstellungen → Integrationen → Slack** auszuführen. Dazu müssen die persönliche OAuth-Callback-URL und die erforderlichen User Token Scopes gehören. Slack kann eine erneute Installation der App verlangen, nachdem Scopes ergänzt wurden.

Wählen Sie nach dem Verbinden die benötigten Slack-Tools aus und binden Sie sie an Ihren privaten Agent. Diese Verbindung ist schreibgeschützt: Sie kann Nachrichten, Kanäle, Dateien und Benutzerinformationen suchen und lesen, aber keine Nachrichten senden, Kanäle erstellen oder Canvases bearbeiten.

## Tools an einen privaten Agent binden

Wählen Sie die Tools unter **Meine Verbindungen → Tools für private Agents** oder im Toolbereich des Agents aus und speichern Sie die Bindung. Der Agent muss Ihre veröffentlichte private Version im aktuellen Team sein. Veröffentlichen Sie eine Agent-Version erneut, bevor Sie ihre persönlichen Tools wieder auswählen. Team-Agents, der Hauptassistent und Agents anderer Personen erhalten niemals Zugriff auf Ihre persönliche Verbindung.

## Jeden Aufruf genehmigen

Entfernte Aufrufe müssen standardmäßig bestätigt werden. Prüfen Sie den Agent, die Verbindung, das Tool, die Zusammenfassung des Vorgangs und alle Parameter genau, insbesondere Daten, die Bowra verlassen.

- **Diesen Aufruf zulassen** führt den Aufruf einmal mit den aktuellen Parametern aus.
- **In dieser Sitzung immer zulassen** erlaubt weitere Aufrufe desselben Tools nur in dieser Sitzung. Die Freigabe endet, wenn die Sitzung beendet oder aktualisiert oder die Verbindung deaktiviert wird.
- **Ablehnen und überspringen** führt den Aufruf nicht aus.
- Aufrufe laufen ab, wenn innerhalb von zwei Minuten keine Entscheidung getroffen wird.

Externe Dienste können nicht rückgängig zu machende Änderungen vornehmen. Dass die Verbindung Ihnen gehört, erlaubt nicht, vertrauliche Teamdaten entgegen den Teamrichtlinien nach außen zu senden.

## Verbindung reparieren, deaktivieren oder löschen

Verwenden Sie **Verbindung bearbeiten / Tools aktualisieren**, um die Authentifizierung zu reparieren oder Tools erneut zu ermitteln. Beim Aktualisieren werden die ausgewählten Tools zurückgesetzt; betroffene Bindungen müssen erneut bestätigt werden. Deaktivieren oder löschen Sie eine Verbindung, um künftige Aufrufe zu verhindern. Ein bereits gestarteter Aufruf kann noch abgeschlossen werden. Beim Löschen werden Zugangsdaten entfernt, während minimale Aufrufprotokolle erhalten bleiben. Vor der Bestätigung zeigt die Seite private Agents an, die noch an Tools dieser Verbindung gebunden sind. Prüfen Sie ein unbekanntes externes Ergebnis beim jeweiligen Dienst, bevor Sie dieselbe Aktion erneut genehmigen.
