# Team-Plugins verwenden

Ein Plugin ist eine wiederverwendbare Auswahl von Tools und Skills. Teamadministratoren können ihm einen Namen und eine Beschreibung geben und verfügbare Ressourcen auswählen. Ein Plugin **Recherche** könnte beispielsweise Websuche-Tools mit einem Recherche-Skill kombinieren.

Integrierte Namen sind standardmäßig Englisch. Bei chinesischer Oberflächensprache werden vorhandene chinesische Bezeichnungen verwendet. Namen, die Sie Ihren eigenen Tools, Skills, Plugins und Agents geben, bleiben unverändert.

## Durchsuchen und verwenden

Teamverantwortliche und Teamadministratoren öffnen **Fähigkeitszentrum → Team-Plugins verwalten**, um die Auswahl des Teams und deren Inhalte anzusehen. Öffnen Sie beim Erstellen oder Bearbeiten eines eigenen Agents **Skills und Tools**, wählen Sie ein Plugin aus und speichern Sie. Sie können auch einzelne Tools und Skills auswählen.

Agents verwenden beim nächsten Start den neuesten Inhalt. Ein bereits laufender Durchlauf behält die Auswahl, mit der er gestartet wurde. Wird ein Plugin ausgeschaltet, laufen Agents, die es verwenden, weiter – nur ohne die Tools und Skills, die es bereitstellt. Einzeln ausgewählte Tools sind nicht betroffen.

Ein Plugin erleichtert die Auswahl von Ressourcen. Ob ein Tool oder Skill tatsächlich ausgeführt werden kann, hängt weiterhin von den aktuellen Berechtigungen und der Verfügbarkeit für die jeweilige Person, den Agent und die Ressource ab.

Wenn Coach verfügbar ist, können Sie im Chat „Füge Notion-Teamwissen zu meinem Recherche-Agenten hinzu“ schreiben. Coach fügt das aktivierte Team-Plugin zum Entwurf Ihres Agenten hinzu. Veröffentlichen Sie den Entwurf im Tab **Veröffentlichen**, um die Änderung zu nutzen. Wenn die Teamverbindung autorisiert oder das Plugin eingerichtet werden muss, übernimmt dies zuerst ein Teamadministrator.

## Antworten aus Notion-Seiten des Teams

Teamverantwortliche und Administratoren können beim Erstellen eines Team-Plugins unter **Mit einer Vorlage beginnen** die Vorlage **Notion-Teamwissen** wählen. Wählen Sie eine Notion-Quelle des Teams. Autorisieren und aktivieren Sie die Quelle unter den Tool-Quellen und importieren Sie die Tools für Suche und Seitenzugriff. Wenn die Quelle Datenbankabfragen anbietet, können Sie dieses Tool ebenfalls importieren. Prüfen Sie die Quelle und die Tools vor dem Speichern. Verwenden Sie ein Notion-Konto, dessen zugängliche Inhalte für die vorgesehenen Teammitglieder bestimmt sind.

Fügen Sie das Plugin Ihrem Agenten hinzu und fragen Sie nach einer Projektentscheidung oder fügen Sie einen Notion-Seitenlink ein. Der Agent verlinkt die Seiten, die seine Antwort stützen. Zum Öffnen eines Links benötigen Sie Zugriff mit Ihrem eigenen Notion-Konto. Wenn die Verbindung endet oder sich Konto oder Workspace ändern, muss ein Administrator die Verbindung und die Tool-Auswahl des Plugins prüfen.

## Plugins und integrierte Tools ausschalten

- **Plugins**: Jedes Plugin hat einen Schalter **Ich**, der nur für dich gilt; über den Hinweis unten kannst du es rückgängig machen. Teamverantwortliche und Teamadministratoren sehen zusätzlich einen Schalter **Team**. Vor einer teamweiten Änderung zeigt Bowra die betroffenen Agents (auch integrierte) und geplanten Aufgaben; die Änderung gilt ab dem nächsten Lauf. Hat das Team ein Plugin ausgeschaltet, können Mitglieder es nicht für sich wieder einschalten.
- **Integrierte Tools**: Unter **Fähigkeitszentrum → Tools** sind integrierte Tools nach Zweck gruppiert. Teamverantwortliche und Teamadministratoren können jedes einzeln ein- oder ausschalten. Ein ausgeschaltetes Tool steht keinem Agent mehr zur Verfügung, auch nicht über ein Plugin. Vor dem Einschalten eines Tools, das Inhalte nach außen schreibt oder sendet (etwa E-Mail senden oder in Google Docs schreiben), müssen Sie das Risiko bestätigen. Über **Details** an einem Tool sehen Sie, wofür es dient, und als Teamverantwortliche oder Teamadministratoren auch, welche Agents und geplanten Aufgaben es nutzen; die Option **Immer bestätigen** wird dort bereits gespeichert, wirkt aber erst, sobald die Bestätigung pro Aufruf verfügbar ist.
- Ausschalten ändert keine Agent-Konfiguration; nach dem Einschalten gilt alles wieder wie zuvor.

## Plugins verwalten

Teamverantwortliche und Teamadministratoren sehen zuerst die bestehende Liste unter **Team-Einstellungen → Plugins**. Gibt es noch keine, zeigt die Seite einen leeren Zustand. Wählen Sie oben rechts **Plugin erstellen**, um eines hinzuzufügen. Wählen Sie zum Bearbeiten den Namen eines Plugins oder **Abbrechen**, um zur Liste zurückzukehren. Geben Sie einen Namen und eine Beschreibung ein, wählen Sie Tools und Skills aus und speichern Sie. Bietet eine integrierte Verbindung eine Vorlage an, etwa Shopify, können Sie sie unter **Mit einer Vorlage beginnen** wählen, um das Formular vorab auszufüllen; gespeichert wird erst, wenn Sie speichern.

Tools und Skills sind in Gruppen aufgeführt: eine Gruppe je Teamverbindung (Tool-Quellen des Teams und aktivierte integrierte Verbindungen zu Drittanbietern), danach die integrierten Tools wie auf der Tools-Seite gruppiert und zuletzt die Skills. Jede Gruppe zeigt, wie viele Einträge ausgewählt sind; Gruppen mit einer Auswahl werden automatisch geöffnet. Über das Suchfeld finden Sie Einträge nach Name, Zweck oder Quelle. Ein Eintrag, der sich nicht auswählen lässt, nennt den Grund; kann ein Administrator ihn beheben, etwa durch Bestätigen der Kategorie eines Tools, verlinkt der Grund auf diese Einstellung. Wird ein Tool oder Skill im Plugin nicht mehr verfügbar, erscheint es oben unter **Nicht mehr verfügbare Auswahl**; entfernen Sie die Auswahl und speichern Sie.

Das Bearbeiten eines bestehenden Plugins aktualisiert die Ressourcen der Agents, die es ausgewählt haben, ab ihrem nächsten Durchlauf. Informieren Sie vor dem Deaktivieren die Eigentümerinnen und Eigentümer der betroffenen Agents, damit sie ihre Konfiguration aktualisieren.

Um ein Plugin endgültig zu entfernen, wählen Sie in seinem Listeneintrag **Löschen**, prüfen Sie die Anzahl betroffener Agents und bestätigen Sie. Tools und Skills dieses Plugins werden sofort aus zukünftigen Durchläufen dieser Agents entfernt. Bereits laufende Durchläufe behalten ihre ursprüngliche Auswahl. Auch Agent-Entwürfe verlieren die Auswahl. Frühere Versionen bleiben sichtbar; durch das Wiederherstellen einer alten Version wird ein gelöschtes Plugin jedoch nicht erneut ausgewählt.

Ein **Outcome** beschreibt ein Ergebnis, das ein Agent Nutzerinnen und Nutzern verspricht, etwa „einen Bericht schreiben“. Es ist unabhängig von der Ressourcenauswahl eines Plugins.
