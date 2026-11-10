# MCP-Tool für das Team hinzufügen

Teamverantwortliche und Teamadministratoren können externe MCP-Dienste als Team-Tools bereitstellen. Mitglieder verwenden dann die freigegebenen Tools entsprechend ihrer Berechtigungen. Für diese Funktion muss die Bereitstellung MCP-Tools unterstützen.

## MCP-Verbindung auf Teamebene einrichten

1. Öffnen Sie **Team-Einstellungen → Tools**.
2. Fügen Sie einen MCP-Dienst mit Namen, Serveradresse und der erforderlichen Authentifizierung hinzu.
3. Lassen Sie Bowra die verfügbaren Tools ermitteln und wählen Sie nur die Tools aus, die das Team benötigt.
4. Speichern Sie die Verbindung und prüfen Sie ihren Status.

Verwenden Sie nur vertrauenswürdige Dienste und Zugangsdaten mit den geringstmöglichen Rechten. Prüfen Sie bei jedem Tool, welche Informationen an den externen Dienst übertragen werden und ob dieser Änderungen ausführen kann. In einer Cloud-Bereitstellung muss der Dienst über öffentliches HTTPS erreichbar sein; interne Bereitstellungen können interne Adressen verwenden.

## Tools für Agents freigeben

Wählen Sie in der Agent-Konfiguration die benötigten Team-Tools aus und veröffentlichen Sie die aktualisierte Agent-Version nach dem vorgesehenen Prüf- und Freigabeprozess. Das Speichern einer Verbindung weist ihre Tools nicht automatisch einem Agent zu. Prüfen Sie mit einem Mitgliedskonto, dass der Agent verfügbar ist und nur die beabsichtigten Aktionen anbietet.

## Zugriff und Verbindung verwalten

Mitglieder können nur Tools verwenden, die für ihren Agent und ihre Rolle freigegeben wurden. Das Freigeben eines Team-Tools verleiht ihnen weder administrative Rechte für den MCP-Dienst noch persönliche Datenberechtigungen. Ändern oder deaktivieren Sie die Verbindung in den Team-Einstellungen, wenn Zugangsdaten erneuert werden müssen oder ein Dienst nicht mehr verwendet werden soll. Prüfen Sie danach betroffene Agents und deren Verfügbarkeit.

## Tool-Liste aktualisieren

Wenn ein MCP-Dienst Tools hinzufügt oder entfernt, wählen Sie in den Aktionen der Quelle **Tool-Liste aktualisieren** (über das Menü **⋯** rechts auf der Karte). Bowra liest zunächst die aktuell vom Dienst angebotenen Tools ein und zeigt die Unterschiede zur gespeicherten Liste:

- **Hinzugefügte Tools**: Tools, die der Dienst jetzt anbietet. Wenn Sie beim Hinzufügen der Quelle nur bestimmte Tools ausgewählt haben, werden neue Tools nicht automatisch importiert.
- **Vom Server entfernte Tools**: Tools, die der Dienst nicht mehr anbietet und die nicht importiert wurden. Ihre Entfernung hat keine Auswirkungen auf Agents.
- **Importierte Tools, die nicht mehr funktionieren**: importierte Tools, die der Dienst nicht mehr anbietet. Nach der Aktualisierung können Agents sie nicht mehr aufrufen. Die Vorschau führt außerdem betroffene Agents, laufende Aufgaben und Zeitpläne auf. Betroffene Zeitpläne werden pausiert, bis ihre Abhängigkeiten erneut bestätigt wurden. Wenn alle importierten Tools entfernt wurden, wird die Quelle deaktiviert.
- **Geänderte Tools**: Tools, die der Dienst weiterhin anbietet, deren Beschreibung oder Parameter sich aber geändert haben. Für jedes Tool sehen Sie, was sich geändert hat und ob es importiert ist. Nach der Aktualisierung verwenden Agents beim nächsten Aufruf eines importierten Tools die neue Version.

Erst wenn Sie **Tool-Liste aktualisieren** auswählen, werden Änderungen übernommen; mit **Abbrechen** bleibt alles unverändert. Ändert sich die Tool-Liste oder ihre Verwendung während Ihrer Prüfung, liest Bowra sie erneut ein und zeigt Ihnen die neuen Änderungen zur Prüfung. Wurde nichts hinzugefügt, entfernt oder geändert, meldet die Vorschau, dass die Tool-Liste auf dem neuesten Stand ist.

Ein MCP-Dienst, der eine Autorisierung erfordert, muss autorisiert sein, bevor Sie seine Tool-Liste aktualisieren können.

## Zu importierende Tools auswählen

Um Tools zu importieren, die Sie beim Hinzufügen der Quelle nicht ausgewählt haben, oder Tools, die eine Aktualisierung gerade hinzugefügt hat, wählen Sie in den Aktionen der Quelle **Tools auswählen** (über das Menü **⋯** rechts auf der Karte). Importiert eine Quelle nur bestimmte Tools und fügt eine Aktualisierung neue hinzu, bietet auch die Bestätigungsmeldung **Tools auswählen** an; neue Tools sind dort mit **Neu** markiert.

Die Liste zeigt alle Tools der zuletzt gespeicherten Tool-Liste; importierte Tools sind angehakt. Haken Sie die Tools an, die Agents verwenden sollen, und wählen Sie **Speichern**. Mindestens ein Tool muss ausgewählt bleiben; wenn kein Tool mehr verwendet werden soll, deaktivieren Sie stattdessen die Quelle.

Wenn Sie derzeit importierte Tools abwählen, zeigt Bowra zuerst die Auswirkungen: die Tools, die entfernt werden, sowie die Agents, laufenden Aufgaben und Zeitpläne, die diese Quelle verwenden. Erst mit **Bestätigen und speichern** wird die Änderung übernommen; **Zurück** führt zur Liste zurück. Nach dem Speichern können Agents die entfernten Tools nicht mehr aufrufen, und betroffene Zeitpläne werden pausiert, bis ihre Abhängigkeiten erneut bestätigt wurden.

## Jeden Aufruf bestätigen, der Daten ändern kann

Wenn Ihre Bereitstellung die Aufrufbestätigung aktiviert, fragt Bowra das Mitglied, das eine Aufgabe gestartet hat, bevor ein MCP-Tool aufgerufen wird, das Daten ändern kann. Der Dialog zeigt die Tool-Quelle, das Tool, seine Beschreibung, ob das Tool Daten schreibt, Daten löscht oder keine bekannte Kategorie hat, und die vollständigen Argumente, die gesendet werden:

- **Diesen Aufruf erlauben** führt diesen Aufruf einmal mit den angezeigten Argumenten aus.
- **In dieser Sitzung immer erlauben** erlaubt auch spätere Aufrufe desselben Tools in der aktuellen Sitzung. Die Freigabe endet mit der Sitzung oder wenn sich die Tooldefinition ändert.
- **Ablehnen und überspringen** führt den Aufruf nicht aus.
- Entscheidet niemand innerhalb von drei Minuten, wird der Aufruf übersprungen.

Wenn Ihre Bereitstellung die Bestätigung im Chat aktiviert, erscheint die Anfrage in der Unterhaltung unter dem wartenden Schritt statt in einem Dialog und bleibt danach als einzeiliger Vermerk Ihrer Entscheidung stehen. Sind Sie auf einer anderen Seite, führt ein Hinweis zur Unterhaltung, und der Titel des Browser-Tabs zeigt, wie viele Aktionen warten.

Tools, die der Dienst als nur lesend kennzeichnet, laufen ohne Rückfrage. Ein Tool, das zugleich als nur lesend und als löschend gekennzeichnet ist, gilt als nicht gekennzeichnet. Tools, die der Dienst als schreibend oder löschend kennzeichnet, können dann für Agents ausgewählt werden und werden bei jedem Aufruf bestätigt. Tools ohne Kennzeichnung können erst für Agents ausgewählt werden, wenn ein Inhaber oder Administrator ihre Kategorie bestätigt; verwendet der Assistent sie, wird jeder Aufruf bestätigt. Jeder Aufruf verwendet das Konto, mit dem die Quelle verbunden wurde, und handelt daher für jedes Mitglied als dieses Konto. Geplante Aufgaben haben niemanden, der bestätigen kann; ihre Aufrufe dieser Tools werden übersprungen.

### Kategorie eines Tools bestätigen

Inhaber und Administratoren können festhalten, was jedes Tool einer MCP-Quelle tut, wenn der Dienst es falsch oder gar nicht kennzeichnet:

1. Wählen Sie in den Aktionen der Quelle (im Menü **⋯** rechts auf der Karte) **Toolkategorien bestätigen**.
2. Wählen Sie für jedes Tool **Nur lesend**, **Schreibt Daten** oder **Löscht Daten**. Kategorien unterhalb der Kennzeichnung des Dienstes werden nicht angeboten.
3. Wählen Sie **Speichern**. Die Änderung gilt ab der nächsten Aufgabe.

Ein als nur lesend bestätigtes Tool läuft ohne Rückfrage, und ein nicht gekennzeichnetes Tool mit bestätigter Kategorie kann für Agents ausgewählt werden. Ändert der Dienst später die Definition eines Tools, gilt dessen Bestätigung nicht mehr und die Liste fordert eine erneute Bestätigung an. Bestätigen Sie **Nur lesend** nur für Tools, die keine Daten ändern können.
