# Persönlichen MCP-Dienst verbinden

Verbinden Sie einen entfernten MCP-Dienst, damit Ihr privater Agent externe Tools bei der Arbeit verwenden kann. Die Verbindung ist nur für Sie sichtbar. Teamverantwortliche und Teamadministratoren können weder die Verbindung einsehen oder konfigurieren noch Aufrufe genehmigen.

## Verbindung hinzufügen und Tools auswählen

Sind unter **Meine Agent-Tools** keine Tools verfügbar, fügen Sie eine Verbindung hinzu oder aktivieren Sie die benötigten Tools in einer bestehenden Verbindung. Klappen Sie dann den gewünschten Agenten auf.

1. Öffnen Sie **Capability Center → Meine Verbindungen** (`/connections/`).
2. Geben Sie einen Namen und die Adresse des entfernten MCP-Dienstes ein. Wählen Sie keine Authentifizierung, Bearer-Token oder API-Schlüssel. Bei einem API-Schlüssel können Sie den vom Dienst geforderten Headernamen verwenden.
3. Wählen Sie **Verbinden und Tools suchen**, markieren Sie die benötigten Tools und speichern Sie.

Gespeicherte Zugangsdaten können nicht erneut angezeigt werden. Lassen Sie die Felder beim Bearbeiten leer, damit die Zugangsdaten erhalten bleiben. Geben Sie neue Zugangsdaten ein, wenn Sie die Authentifizierungsmethode ändern. Bowra unterstützt entfernte MCP-Dienste über HTTP(S), keine lokalen Befehle. Cloud-Bereitstellungen erfordern öffentliches HTTPS; bei internen Bereitstellungen sind interne Adressen zulässig. Verwenden Sie nur vertrauenswürdige Dienste und Zugangsdaten mit den geringstmöglichen Rechten.

## Mit dem eigenen Konto bei einem Dienst anmelden

Manche MCP-Dienste stellen kein Token bereit, sondern verlangen eine Anmeldung mit Ihrem eigenen Konto, zum Beispiel Notion, Linear, Sentry oder n8n. Wählen Sie unter **Authentifizierung** die Option **Beim Dienst anmelden**. Belassen Sie **Keine Authentifizierung** und verlangt der Dienst eine Anmeldung, wechselt der Dialog dorthin und nennt, wo Sie sich anmelden.

1. Geben Sie einen Namen und die Remote-MCP-Adresse ein und wählen Sie **Weiter zur Anmeldung**.
2. Melden Sie sich beim Dienst an und genehmigen Sie den angeforderten Zugriff. Manche Dienste erlauben es, die Rechte von Bowhead einzuschränken (zum Beispiel nur Lesen); die später verfügbaren Tools richten sich nach dieser Wahl.
3. Wählen Sie zurück unter **Meine Verbindungen** die benötigten Tools aus und speichern Sie. Die Verbindung zeigt, wo Sie angemeldet sind.

Bowhead speichert Ihre Anmeldung nur für diese Verbindung und erneuert sie bei Bedarf; sie bleibt nach Neustarts erhalten und wird niemandem angezeigt. Beendet der Dienst die Anmeldung (zum Beispiel nach langer Nichtnutzung oder weil der Zugriff von Bowhead dort entfernt wurde), zeigt die Verbindung an, dass Ihre Anmeldung abgelaufen ist: Wählen Sie **Erneut anmelden**. Ihre Tool-Auswahl und Agent-Bindungen bleiben erhalten; Tools mit geänderter Definition müssen erneut ausgewählt werden. Bis dahin laufen Ihre Agents mit ihren anderen Tools weiter, und ein Aufruf der Tools dieser Verbindung wird nicht gesendet: Der Agent bittet Sie, sich erneut anzumelden.

Eine abgebrochene oder nicht abgeschlossene Anmeldung zeigt **Anmeldung nicht abgeschlossen** mit **Anmeldung fortsetzen**. Bei einer angemeldeten Verbindung lässt sich nur der Name ändern; für eine andere Adresse fügen Sie eine neue Verbindung hinzu. Beim Löschen vergisst Bowhead Ihre Anmeldung; um auch beim Dienst den Zugriff von Bowhead zu entfernen, entfernen Sie Bowhead dort aus den verbundenen Apps.

Manche Dienste akzeptieren nur Apps, die ihre Administration registriert hat (zum Beispiel GitHub, Google und Microsoft). Bei diesen kann sich Bowhead noch nicht anmelden; verwenden Sie ein persönliches Token oder einen API-Schlüssel, falls der Dienst diese anbietet.

| Situation                                                                       | Wo verbinden                                       | Wessen Konto der Dienst sieht                      |
| ------------------------------------------------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| Das Team teilt ein Dienstkonto, das eine Administration verwaltet               | Fähigkeiten → Tools → Werkzeugquellen verwalten    | Die Anmeldung der Administration, vom Team geteilt |
| Jede Person soll in eigenem Namen handeln, und der Dienst bietet eine Anmeldung | Meine Verbindungen → Beim Dienst anmelden          | Ihr eigenes                                        |
| Der Dienst gibt Ihnen ein persönliches Token oder einen API-Schlüssel           | Meine Verbindungen → Bearer Token / API Key        | Ihr eigenes                                        |
| Slack                                                                           | Meine Verbindungen → Eigenes Slack-Konto verbinden | Ihr eigenes, über die Slack-App des Teams          |

## Eigenes Slack-Konto verbinden

Wenn die Teamadministration Slack aktiviert und die Bereitstellung diese Funktion freigeschaltet hat, erscheint **Mein Slack verbinden** unter **Meine Verbindungen**. Schließen Sie die Autorisierung mit Ihrem eigenen Slack-Konto ab. Verbindung und Token gehören nur Ihnen; Teamverantwortliche und Teamadministratoren können sie weder einsehen noch verwenden.

Nach der Rückkehr zu **Meine Verbindungen** erklärt die Seite, ob die Verbindung erfolgreich war und was Sie bei einem Fehler prüfen oder erneut versuchen können. Falls es weiterhin nicht funktioniert, bitten Sie eine teamadministrierende Person, den letzten Schritt **Persönliches Slack-MCP aktivieren** unter **Team-Einstellungen → Integrationen → Slack** auszuführen. Dazu müssen die persönliche OAuth-Callback-URL und die erforderlichen User Token Scopes gehören. Slack kann eine erneute Installation der App verlangen, nachdem Scopes ergänzt wurden.

Wählen Sie nach dem Verbinden die benötigten Slack-Tools aus und binden Sie sie an Ihren privaten Agent. Diese Verbindung ist schreibgeschützt: Sie kann Nachrichten, Kanäle, Dateien und Benutzerinformationen suchen und lesen, aber keine Nachrichten senden, Kanäle erstellen oder Canvases bearbeiten.

## Tools an einen privaten Agent binden

Wählen Sie die Tools unter **Meine Verbindungen → Tools für private Agents** oder im Toolbereich des Agents aus und speichern Sie die Bindung. Der Agent muss Ihre veröffentlichte private Version im aktuellen Team sein. Veröffentlichen Sie eine Agent-Version erneut, bevor Sie ihre persönlichen Tools wieder auswählen. Team-Agents, der Hauptassistent und Agents anderer Personen erhalten niemals Zugriff auf Ihre persönliche Verbindung.

## Jeden Aufruf genehmigen

Entfernte Aufrufe müssen standardmäßig bestätigt werden. Die Anfrage erscheint in der Unterhaltung unter dem wartenden Schritt. Prüfen Sie den Agent, die Verbindung, das Tool, die Zusammenfassung des Vorgangs und alle Parameter genau, insbesondere Daten, die Bowra verlassen.

- **Diesen Aufruf zulassen** führt den Aufruf einmal mit den aktuellen Parametern aus.
- **In dieser Sitzung immer zulassen** erlaubt weitere Aufrufe desselben Tools nur in dieser Sitzung. Die Freigabe endet, wenn die Sitzung beendet oder aktualisiert oder die Verbindung deaktiviert wird.
- **Ablehnen und überspringen** führt den Aufruf nicht aus.
- Aufrufe laufen ab, wenn innerhalb von drei Minuten keine Entscheidung getroffen wird.

Nach Ihrer Entscheidung bleibt die Anfrage als einzeiliger Vermerk Ihrer Entscheidung in der Unterhaltung stehen. Sind Sie auf einer anderen Seite, führt ein Hinweis zur Unterhaltung, und der Titel des Browser-Tabs zeigt, wie viele Aktionen warten.

Externe Dienste können nicht rückgängig zu machende Änderungen vornehmen. Dass die Verbindung Ihnen gehört, erlaubt nicht, vertrauliche Teamdaten entgegen den Teamrichtlinien nach außen zu senden.

## Verbindung reparieren, deaktivieren oder löschen

Verwenden Sie **Verbindung bearbeiten / Tools aktualisieren**, um die Authentifizierung zu reparieren oder Tools erneut zu ermitteln. Lehnt der Dienst die Zugangsdaten einer Verbindung ab, zeigt die Verbindung an, dass die Authentifizierung abgelaufen ist; Ihre Agents laufen mit ihren anderen Tools weiter, und ein Aufruf der Tools dieser Verbindung wird nicht gesendet: Der Agent bittet Sie, zuerst die Zugangsdaten zu aktualisieren. Beim Aktualisieren werden die ausgewählten Tools zurückgesetzt; betroffene Bindungen müssen erneut bestätigt werden. Deaktivieren oder löschen Sie eine Verbindung, um künftige Aufrufe zu verhindern. Ein bereits gestarteter Aufruf kann noch abgeschlossen werden. Beim Löschen werden Zugangsdaten entfernt, während minimale Aufrufprotokolle erhalten bleiben. Vor der Bestätigung zeigt die Seite private Agents an, die noch an Tools dieser Verbindung gebunden sind. Prüfen Sie ein unbekanntes externes Ergebnis beim jeweiligen Dienst, bevor Sie dieselbe Aktion erneut genehmigen.
