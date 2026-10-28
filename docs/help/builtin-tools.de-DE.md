# Integrierte Tools von Drittanbietern

Teamverantwortliche und Teamadministratoren können die von Bowra unterstützten Tools von Drittanbietern unter **Team-Einstellungen → Tool-Quellen** verbinden. Derzeit werden Tavily, Datarc und Power BI unterstützt. Im Auswahlfeld und in der Liste der konfigurierten Tools ist jedes Angebot mit einem eigenen Symbol gekennzeichnet.

## Tavily

Wählen Sie **＋ Integriertes Drittanbieter-Tool hinzufügen**, dann Tavily. Fügen Sie den API-Schlüssel ein und wählen Sie **Prüfen und speichern**. Bowra prüft den Schlüssel und speichert ihn anschließend verschlüsselt. Nach dem Speichern können Sie den Schlüssel jederzeit bearbeiten und das Tool ein- oder ausschalten.

Ist Tavily aktiviert, können Team-Agents mit Websuche das Tool verwenden. Ist es deaktiviert, verwenden neue Suchanfragen Tavily nicht mehr. Der API-Schlüssel wird Teammitgliedern und Agents niemals angezeigt.

### Im Web suchen

Geben Sie in Ihrer Frage an, worauf die Suche eingegrenzt werden soll. Der Agent filtert die Ergebnisse entsprechend, zum Beispiel auf „nur Nachrichten aus der letzten Woche“, „nur Regierungswebsites“, „eine bestimmte Website ausschließen“ oder „gründlicher suchen“. Nachrichtenergebnisse enthalten ihr Veröffentlichungsdatum.

### Webseiten lesen und Websites durchsuchen

Wenn diese Funktionen in Ihrer Bereitstellung aktiviert sind (unter **Tool hinzufügen** für einen Agent sind dann beispielsweise Optionen wie **Webseiten extrahieren** zu sehen), kann Tavily außerdem den Text mehrerer öffentlicher Webseiten gleichzeitig lesen, Seiten einer Website auflisten und anschließend die gewünschten Seiten lesen oder Links folgen, um mehrere Seiten einer Website abzurufen. Beispiele: „Lies diese drei Links und fasse sie zusammen“, „Welche Produktseiten gibt es auf dieser Website?“ oder „Lies alle Installationsanleitungen auf dieser Dokumentationswebsite.“ Eine Website-Suche ruft standardmäßig etwa 20 Seiten ab (höchstens 100), kann zwei bis drei Minuten dauern und verbraucht mehr Credits. Wenn Sie nur wenige Seiten benötigen, geht es schneller, die Links direkt anzugeben.

Damit keine Informationen an unbekannte Websites gesendet werden, liest der Agent nur Links, die Sie im Chat angegeben haben, Links aus Suchergebnissen sowie Seiten, die auf derselben Website aufgelistet oder abgerufen wurden. Links auf diesen Seiten, die zu anderen Websites führen, werden zwar angezeigt, aber nicht gelesen. Auch die Recherche bleibt auf Websites beschränkt, die Sie genannt haben; zuvor entfernt der Agent unbekannte Webadressen aus der Frage. Interne Adressen und Seiten, für die eine Anmeldung erforderlich ist, gehören nicht zu diesen Funktionen. Der Agent öffnet solche Seiten stattdessen direkt.

### Tiefgehende Recherche

Auch diese Funktion ist nur verfügbar, wenn sie in Ihrer Bereitstellung aktiviert wurde. Für eine systematische externe Recherche können Sie ausdrücklich eine tiefgehende Recherche anfordern, zum Beispiel: „Recherchiere gründlich öffentliche Nachrichten über chinesische Hersteller von Elektroautos, die in den letzten sechs Monaten ins Ausland expandiert sind.“ Der Agent übergibt die Frage an die Recherche-Engine von Tavily und erhält einige Minuten später einen Bericht mit Quellenlinks. Währenddessen zeigt der Chat an, dass dieser Schritt läuft. Pro Chat-Turn ist höchstens eine tiefgehende Recherche möglich. Der kostenintensivere Modus wird nur verwendet, wenn Sie ausdrücklich um die umfassendste Recherche bitten.

### Funktionen aktivieren

Webseiten lesen, Websites durchsuchen und tiefgehende Recherche werden für jeden Agent separat aktiviert. Die integrierten Agents für Datenanalyse und Marketinganalyse können standardmäßig Webseiten lesen und Websites durchsuchen. Tiefgehende Recherche ist standardmäßig nur für den Agent für Marketinganalyse verfügbar. Öffnen Sie bei selbst erstellten Agents **Konfigurieren → Was er nutzen kann → Hinzufügen… → Tool hinzufügen** und wählen Sie **Webseiten extrahieren**, **Website erfassen**, **Website durchsuchen** und **Tiefgehende Recherche** aus.

### Credits

Jede Suche, jeder Lesevorgang, jeder Crawl und jede Recherche verbraucht Credits aus dem Tavily-Konto des Teams. „Tiefere“ Suchen und ausführliche Seitenabrufe kosten doppelt so viel, Crawls werden pro Seite abgerechnet und tiefgehende Recherchen kosten deutlich mehr als eine normale Suche. Wenn die Credits aufgebraucht oder der Schlüssel ungültig ist, weist der Agent in seiner Antwort darauf hin, dass die Websuche vorübergehend nicht verfügbar ist, und empfiehlt, eine Teamadministration zu kontaktieren. Die Administration kann in der Tavily-Konsole Credits aufladen oder den Schlüssel ersetzen und ihn anschließend auf dieser Seite aktualisieren. Ein Schlüssel ohne verbleibende Credits lässt sich weiterhin speichern und funktioniert wieder, sobald Credits verfügbar sind.

## Datarc

Wählen Sie **＋ Integriertes Drittanbieter-Tool hinzufügen → Datarc**. Geben Sie einen Konfigurationsnamen, den Endpunkt und den Verbindungsschlüssel ein. Der Endpunkt ist die Stammadresse des Datarc-Dienstes, zum Beispiel `https://openapi.datarc.cn`; geben Sie keinen bestimmten API-Pfad ein. Nach erfolgreicher Prüfung wird der Schlüssel verschlüsselt gespeichert. In der Liste erscheint die Verbindung als „Datarc – Konfigurationsname“.

Ein Team kann mehrere Datarc-Verbindungen hinzufügen und sie in den Tool-Einstellungen von Agents getrennt zuweisen. Weisen Sie beispielsweise „Datarc – Finanzen“ einem Finanz-Agent und „Datarc – Vertrieb“ einem Vertriebs-Agent zu. Wenn Sie eine Verbindung umbenennen, bleiben bestehende Zuweisungen erhalten. Wird eine Verbindung deaktiviert oder gelöscht, wechseln Agents nicht automatisch zu einer anderen Verbindung.

Beim Erstellen oder Bearbeiten einer Verbindung liest Bowra die API-Berechtigungen des Schlüssels, die erreichbaren Arbeitsbereiche und das Ablaufdatum aus. Im Bearbeitungsfenster werden die Ergebnisse nach Berechtigungen, verfügbaren Tools, Arbeitsbereichen und Gültigkeit gruppiert angezeigt. Die Liste der Arbeitsbereiche ist standardmäßig eingeklappt und lässt sich ausklappen. Wählen Sie **Erneut prüfen**, um den Umfang zu aktualisieren. Eine ältere Verbindung stellt Agents erst nach ihrer ersten erfolgreichen Prüfung Tools bereit. Das gilt auch, nachdem die Schlüsselprüfung fehlgeschlagen oder der Schlüssel abgelaufen ist.

Einige ältere Datarc-Versionen geben die Schlüsselberechtigungen nicht zurück. In diesen Fällen prüft Bowra den Schlüssel, indem es die erreichbaren Arbeitsbereiche ausliest, zeigt die Berechtigungen als **Alle Berechtigungen** an und stellt alle drei Funktionen bereit. Ob eine Abfrage tatsächlich funktioniert, hängt weiterhin von den Berechtigungen des Schlüssels in Datarc ab. Wenn die Prüfung des Verbindungsschlüssels fehlschlägt, kontrollieren Sie, ob der Schlüssel korrekt und noch gültig ist. Falls gemeldet wird, dass der Endpunkt keine Datarc-API bereitstellt, prüfen Sie, ob Sie die Stammadresse des Datarc-Dienstes eingegeben haben.

Je nach Berechtigungen des Schlüssels stellt Bowra **Arbeitsbereiche auflisten**, **Datenschema prüfen** und **Daten abfragen** bereit. Diese Funktionen gehören zu einer Verbindung und müssen nicht einzeln für einen Agent eingerichtet werden.

## Power BI

Wählen Sie **＋ Integriertes Drittanbieter-Tool hinzufügen → Power BI**, benennen Sie die Verbindung und geben Sie die Microsoft-Entra-Mandanten-ID, die Client-ID und das Client-Geheimnis ein. Sie können eine Arbeitsbereich-ID und eine semantische Modell-ID angeben oder beide Felder leer lassen, damit automatisch ein zugängliches Modell ausgewählt wird. Bowra prüft den Abfragezugriff vor dem Speichern. Das Geheimnis wird verschlüsselt gespeichert und danach nicht mehr angezeigt. Ein Team kann mehrere Verbindungen hinzufügen, beispielsweise getrennte Finanz- und Vertriebsmodelle.

Eine Organisationsadministration muss die Anwendung in Microsoft Entra registrieren, das Client-Geheimnis erstellen, der Sicherheitsgruppe des Dienstprinzipals die Nutzung der Power-BI-API erlauben, den Dienstprinzipal zum Zielarbeitsbereich hinzufügen und ihm die Build-Berechtigung für das semantische Modell erteilen. Schlägt die Prüfung fehl, kontrollieren Sie Mandant, Arbeitsbereich, Modell und Build-Berechtigung.

Jede Verbindung hat eigene DAX-Vorlagen. Teamverantwortliche und Teamadministratoren können sie auf der Verbindungskarte hinzufügen oder entfernen. Wenn eine Verbindung deaktiviert oder gelöscht wird, wechselt ein Agent nicht automatisch zu einer anderen. Beim Löschen der Verbindung werden auch ihre Vorlagen gelöscht.

Wählen Sie im Abschnitt **Konfigurieren → Was er nutzen kann** des Agents die gewünschte Power-BI-Verbindung aus und speichern Sie. Jede Verbindung wird einzeln aufgeführt. Agents können nur die ihnen zugewiesenen Verbindungen verwenden. Bestehende Verbindungen mit Dienstprinzipal wurden zu den Tool-Quellen migriert. Eine ältere Verbindung mit delegierter Autorisierung muss mit einem Dienstprinzipal neu eingerichtet werden. Wählen Sie anschließend die Verbindung in den Agent-Einstellungen aus und speichern Sie. Power BI prüft die Berechtigungen des Dienstprinzipals des Teams und nicht die der einzelnen Person im Chat.

## Agents zuweisen

Öffnen Sie **Agent-Verwaltung → Von mir erstellt → Konfigurieren → Was er nutzen kann**, wählen Sie **Hinzufügen…** und dann **Tool hinzufügen**. Team-Tools erscheinen als Verbindungskarten:

- Die Karte zeigt den Konfigurationsnamen, zum Beispiel „Datarc – Finanzen“.
- Mit einem Klick auf eine Karte fügen Sie die gesamte Verbindung hinzu.
- Klappen Sie **Enthaltene Funktionen anzeigen** auf, um die Tools der Verbindung zu sehen.
- Die Suche berücksichtigt sowohl Verbindungsnamen als auch Funktionsnamen.
- **Team-Tool-Verbindungen verwalten** führt Sie zur Tool-Seite in den Team-Einstellungen.

Beim Speichern des Agents übernimmt Bowra die konkreten Tools aus der zuletzt gespeicherten Prüfung der Verbindung. Bowra prüft den Umfang nicht bei jedem Aufruf erneut und erweitert ihn auch nicht. Der Umfang wird erst aktualisiert, wenn Sie den Agent erneut speichern. Später hinzugefügte Berechtigungen des externen Schlüssels werden nicht automatisch zu bestehenden Agents hinzugefügt. Ist eine API auf der externen Seite nicht verfügbar, schlagen Aufrufe an sie fehl.

Angenommen, das Team hat zwei Verbindungen:

```text
Datarc – Finanzen
  Arbeitsbereiche auflisten, Datenschema prüfen, Daten abfragen

Datarc – Vertrieb
  Arbeitsbereiche auflisten, Daten abfragen
```

Auf der Agent-Seite werden zwei Verbindungskarten statt fünf ähnlich benannter Tools angezeigt. Jedes Tool bleibt an seine eigene Verbindung gebunden. Die Verbindungen verwenden niemals denselben Endpunkt oder Schlüssel.

Beim Abfragen von Daten wird der tatsächliche Umfang durch den Verbindungsschlüssel, die aktuellen Mitgliedschaftsberechtigungen der Person in Datarc und die für den Agent gespeicherten Tools begrenzt. Ist nur ein Arbeitsbereich zugänglich, wird er automatisch ausgewählt. Sind mehrere zugänglich, bittet der Agent um eine Auswahl. Wenn kein Datarc-Konto gefunden wird, prüfen Sie, ob Ihre Bowra-Anmelde-E-Mail in dieser Datarc-Instanz eingerichtet und zum Zielarbeitsbereich hinzugefügt wurde.

Verbindungsschlüssel werden Teammitgliedern und Agents niemals angezeigt. Private Bereitstellungen können einen internen Firmenendpunkt verwenden; SaaS-Bereitstellungen akzeptieren ausschließlich öffentliche HTTPS-Adressen.
